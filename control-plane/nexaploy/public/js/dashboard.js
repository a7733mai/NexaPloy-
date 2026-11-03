/* Nexaploy customer portal (served as /assets/nexaploy/js/dashboard.js, no build step).
 *
 * - nx.call(method, args): POST /api/method/<method> with the Frappe CSRF token,
 *   resolves to `message`, rejects with an Error carrying the server's message.
 * - nx.pollJob(name, onUpdate): polls nexaploy.api.dashboard.get_job until the job is terminal.
 * - nx.toast(message, kind)
 * - __(): translations from NX_BOOT.messages (this app's ar.csv), "{0}" placeholders.
 * - Alpine.data components for each /dashboard page (Alpine.js 3.14.1 is loaded after this file).
 */
(function () {
	"use strict";

	var BOOT = window.NX_BOOT || {};
	var MESSAGES = BOOT.messages || {};
	var API = "nexaploy.api.dashboard.";
	var TERMINAL = ["Success", "Failure"];
	var ACTION_LABELS = {
		backup: "Backup",
		install_app: "Install app",
		uninstall_app: "Uninstall app",
		add_domain: "Add domain",
		remove_domain: "Remove domain",
		migrate: "Migrate",
		drop: "Drop site",
		restore: "Restore",
	};

	function format(str, args) {
		if (!args) return str;
		if (!Array.isArray(args)) args = [args];
		return String(str).replace(/\{(\d+)\}/g, function (m, i) {
			return args[i] !== undefined && args[i] !== null ? args[i] : m;
		});
	}

	function translate(msg, args) {
		if (msg === undefined || msg === null) return "";
		return format(MESSAGES[msg] || msg, args);
	}
	// Frappe's own __ is not loaded on these pages; never clobber it if it is.
	if (typeof window.__ !== "function") window.__ = translate;
	var __ = window.__;

	function stripHtml(html) {
		var div = document.createElement("div");
		div.innerHTML = html;
		return (div.textContent || div.innerText || "").trim();
	}

	function serverMessage(data) {
		if (data && data._server_messages) {
			try {
				var msgs = JSON.parse(data._server_messages).map(function (m) {
					try {
						var o = JSON.parse(m);
						return o.message || m;
					} catch (e) {
						return m;
					}
				});
				if (msgs.length) return stripHtml(msgs.join("\n"));
			} catch (e) { /* fall through */ }
		}
		if (data && data.exception) {
			var parts = String(data.exception).split(":");
			return stripHtml(parts.length > 1 ? parts.slice(1).join(":") : parts[0]);
		}
		if (data && data.message && typeof data.message === "string") return stripHtml(data.message);
		return null;
	}

	function call(method, args) {
		var headers = {
			"Accept": "application/json",
			"Content-Type": "application/json",
			"X-Requested-With": "XMLHttpRequest",
		};
		if (window.frappe && window.frappe.csrf_token) headers["X-Frappe-CSRF-Token"] = window.frappe.csrf_token;
		return fetch("/api/method/" + method, {
			method: "POST",
			headers: headers,
			credentials: "same-origin",
			body: JSON.stringify(args || {}),
		}).then(
			function (res) {
				return res.json().catch(function () { return {}; }).then(function (data) {
					if (!res.ok || data.exc || data.exc_type) {
						var err = new Error(serverMessage(data) || __("Request failed ({0})", [res.status]));
						err.status = res.status;
						err.excType = data.exc_type;
						if (res.status === 403 && /login/i.test(err.message)) {
							window.location.href = "/login?redirect-to=" + encodeURIComponent(location.pathname + location.search);
						}
						throw err;
					}
					return data.message;
				});
			},
			function () {
				throw new Error(__("Network error. Check your connection."));
			}
		);
	}

	function api(method, args) {
		return call(API + method, args);
	}

	function toast(message, kind, timeout) {
		var box = document.getElementById("nx-toasts");
		if (!box) return;
		var el = document.createElement("div");
		el.className = "nx-toast " + (kind || "info");
		el.setAttribute("role", kind === "error" ? "alert" : "status");
		el.textContent = message;
		box.appendChild(el);
		setTimeout(function () { el.remove(); }, timeout || (kind === "error" ? 8000 : 4000));
	}

	/* Poll an Agent Job until it is terminal. Returns {stop(), done: Promise<job>}. */
	function pollJob(name, onUpdate, interval) {
		var stopped = false;
		var timer = null;
		var resolveDone;
		var done = new Promise(function (resolve) { resolveDone = resolve; });
		function tick() {
			if (stopped) return;
			api("get_job", { name: name }).then(
				function (job) {
					if (stopped) return;
					if (onUpdate) onUpdate(job);
					if (TERMINAL.indexOf(job.status) !== -1) {
						stopped = true;
						resolveDone(job);
					} else {
						timer = setTimeout(tick, interval || 2500);
					}
				},
				function (err) {
					if (stopped) return;
					timer = setTimeout(tick, (interval || 2500) * 2);
					if (err.status === 403 || err.status === 404) {
						stopped = true;
						resolveDone(null);
					}
				}
			);
		}
		tick();
		return {
			stop: function () { stopped = true; clearTimeout(timer); resolveDone(null); },
			done: done,
		};
	}

	function copy(text) {
		var ok = function () { toast(__("Copied to clipboard"), "success", 2000); };
		if (navigator.clipboard && window.isSecureContext) {
			return navigator.clipboard.writeText(text).then(ok, function () { fallbackCopy(text, ok); });
		}
		fallbackCopy(text, ok);
	}

	function fallbackCopy(text, ok) {
		var ta = document.createElement("textarea");
		ta.value = text;
		ta.style.position = "fixed";
		ta.style.opacity = "0";
		document.body.appendChild(ta);
		ta.select();
		try { document.execCommand("copy"); ok(); } catch (e) { toast(__("Copy failed"), "error"); }
		ta.remove();
	}

	function parseDate(value) {
		if (!value) return null;
		if (value instanceof Date) return value;
		var d = new Date(String(value).replace(" ", "T"));
		return isNaN(d.getTime()) ? null : d;
	}

	function fmtDate(value) {
		var d = parseDate(value);
		if (!d) return "";
		try {
			return new Intl.DateTimeFormat(BOOT.lang || undefined, { dateStyle: "medium", timeStyle: "short" }).format(d);
		} catch (e) {
			return d.toLocaleString();
		}
	}

	function ago(seconds) {
		if (seconds === null || seconds === undefined) return __("never");
		if (seconds < 45) return __("just now");
		if (seconds < 3600) return __("{0} min ago", [Math.round(seconds / 60)]);
		if (seconds < 86400) return __("{0} h ago", [Math.round(seconds / 3600)]);
		return __("{0} d ago", [Math.round(seconds / 86400)]);
	}

	function since(value) {
		var d = parseDate(value);
		return d ? ago(Math.max(0, (Date.now() - d.getTime()) / 1000)) : __("never");
	}

	function bytes(n) {
		if (n === null || n === undefined || n === "") return "";
		n = Number(n);
		var units = ["B", "KB", "MB", "GB", "TB"];
		var i = 0;
		while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
		return (i ? n.toFixed(1) : n) + " " + units[i];
	}

	function money(amount, currency) {
		if (amount === null || amount === undefined || amount === "") return "";
		try {
			return new Intl.NumberFormat(BOOT.lang || undefined, { style: "currency", currency: currency || "USD" }).format(amount);
		} catch (e) {
			return (currency || "") + " " + amount;
		}
	}

	var STATUS_CLASS = {
		Active: "success", Success: "success", Installed: "success", Paid: "success",
		Pending: "info", Queued: "info", Running: "info pulse", Building: "info pulse", Moving: "info pulse",
		Removing: "info pulse", Draft: "info", pending: "info", running: "info pulse", success: "success",
		Offline: "warning", Suspended: "warning", Unpaid: "warning", "Partly Paid": "warning",
		Broken: "danger", Failure: "danger", Failed: "danger", Overdue: "danger", failure: "danger",
		Archived: "", Cancelled: "", skipped: "",
	};

	function statusClass(status) {
		var cls = STATUS_CLASS[status];
		return "badge " + (cls === undefined ? "" : cls);
	}

	function flatten(obj, prefix, out) {
		out = out || [];
		if (obj === null || obj === undefined) return out;
		Object.keys(obj).forEach(function (k) {
			var key = prefix ? prefix + "." + k : k;
			var v = obj[k];
			if (v && typeof v === "object" && !Array.isArray(v)) flatten(v, key, out);
			else out.push([key, Array.isArray(v) ? v.map(function (x) { return typeof x === "object" ? JSON.stringify(x) : x; }).join(", ") : v]);
		});
		return out;
	}

	function query(name) {
		return new URLSearchParams(window.location.search).get(name);
	}

	/* Billing (nexaploy.api.billing, optional): accept a list or {invoices: [...]} */
	function invoiceList(r) {
		if (Array.isArray(r)) return r;
		if (r && Array.isArray(r.invoices)) return r.invoices;
		if (r && Array.isArray(r.data)) return r.data;
		return [];
	}
	function invoiceAmount(inv) {
		var v = [inv.outstanding_amount, inv.amount_due, inv.grand_total, inv.total, inv.amount];
		for (var i = 0; i < v.length; i++) if (v[i] !== undefined && v[i] !== null) return v[i];
		return null;
	}
	function isUnpaid(inv) {
		var s = String(inv.status || "");
		if (["Paid", "Cancelled", "Void", "Draft"].indexOf(s) !== -1) return false;
		if (["Unpaid", "Overdue", "Partly Paid", "Open", "Due"].indexOf(s) !== -1) return true;
		return Number(inv.outstanding_amount || inv.amount_due || 0) > 0;
	}

	/* nexaploy.api.billing.get_invoices (older drafts: list_invoices) */
	function fetchInvoices() {
		return call("nexaploy.api.billing.get_invoices").catch(function (e) {
			if (!isMissingMethod(e)) throw e;
			return call("nexaploy.api.billing.list_invoices");
		});
	}
	function isMissingMethod(e) {
		return e.status === 404 || /not found|has no attribute|No module|Failed to get method|not whitelisted/i.test(e.message || "");
	}

	function siteNamePreview(value, root) {
		var text = String(value || "").trim().toLowerCase().replace(/^\.+|\.+$/g, "");
		if (!text) return "";
		if (text.indexOf(".") !== -1) return text;
		text = text.replace(/[\s_.]+/g, "-").replace(/[^a-z0-9-]/g, "").replace(/-{2,}/g, "-").replace(/^-+|-+$/g, "");
		return text ? text + "." + root : "";
	}

	window.nx = {
		boot: BOOT, call: call, api: api, toast: toast, pollJob: pollJob, copy: copy, fmtDate: fmtDate,
		ago: ago, since: since, bytes: bytes, money: money, statusClass: statusClass, flatten: flatten,
		query: query, invoiceList: invoiceList, invoiceAmount: invoiceAmount, isUnpaid: isUnpaid,
		siteNamePreview: siteNamePreview, format: format,
	};

	/* ------------------------------------------------------------------ job viewer mixin */
	function jobViewer() {
		return {
			openJob: null,
			jobDetail: null,
			openSteps: {},
			_poller: null,
			toggleJob: function (name) {
				if (this._poller) this._poller.stop();
				if (this.openJob === name) {
					this.openJob = null;
					this.jobDetail = null;
					return;
				}
				this.openJob = name;
				this.jobDetail = null;
				this.openSteps = {};
				var self = this;
				this._poller = pollJob(name, function (job) {
					self.jobDetail = job;
					(job.steps || []).forEach(function (s) {
						if (s.status === "running" || s.status === "failure") self.openSteps[s.idx] = true;
					});
				});
				this._poller.done.then(function (job) {
					if (job && self.onJobFinished) self.onJobFinished(job);
				});
			},
			toggleStep: function (idx) {
				this.openSteps[idx] = !this.openSteps[idx];
			},
			stopJobPoll: function () {
				if (this._poller) this._poller.stop();
			},
		};
	}

	function mixin(target, source) {
		Object.keys(source).forEach(function (k) {
			var desc = Object.getOwnPropertyDescriptor(source, k);
			Object.defineProperty(target, k, desc);
		});
		return target;
	}

	/* ------------------------------------------------------------------ pages */
	document.addEventListener("alpine:init", function () {
		var Alpine = window.Alpine;

		Alpine.data("nxOverview", function () {
			return {
				loading: true,
				error: null,
				data: null,
				billing: null,
				timer: null,
				init: function () {
					var self = this;
					this.load();
					this.loadBilling();
					this.timer = setInterval(function () { self.load(true); }, 15000);
				},
				destroy: function () { clearInterval(this.timer); },
				load: function (quiet) {
					var self = this;
					if (!quiet) this.loading = true;
					return api("overview").then(
						function (r) { self.data = r; self.error = null; },
						function (e) { if (!quiet) self.error = e.message; }
					).finally(function () { self.loading = false; });
				},
				loadBilling: function () {
					var self = this;
					fetchInvoices().then(
						function (r) {
							var list = invoiceList(r);
							self.billing = { unpaid: list.filter(isUnpaid).length, total: list.length };
						},
						function () { self.billing = null; }
					);
				},
				count: function (group, status) {
					return (this.data && this.data[group].by_status[status]) || 0;
				},
			};
		});

		Alpine.data("nxSites", function () {
			return {
				loading: true,
				error: null,
				sites: [],
				q: "",
				showNew: false,
				benches: null,
				plans: [],
				form: { bench: "", name: "", apps: [], plan: "" },
				creating: false,
				created: null,
				root: BOOT.root_domain || "nexaploy.cloud",
				init: function () {
					this.load();
					if (query("new") || location.hash === "#new") this.openNew();
				},
				load: function () {
					var self = this;
					this.loading = true;
					return api("list_sites").then(
						function (r) { self.sites = r; self.error = null; },
						function (e) { self.error = e.message; }
					).finally(function () { self.loading = false; });
				},
				get filtered() {
					var q = this.q.trim().toLowerCase();
					if (!q) return this.sites;
					return this.sites.filter(function (s) { return s.name.indexOf(q) !== -1 || (s.bench || "").indexOf(q) !== -1; });
				},
				openNew: function () {
					var self = this;
					this.showNew = true;
					this.created = null;
					if (this.benches) return;
					Promise.all([api("list_benches", { server: query("server") || null }), api("list_plans", { plan_type: "Site" })]).then(
						function (r) {
							self.benches = r[0];
							self.plans = r[1];
							if (self.benches.length === 1) self.selectBench(self.benches[0].name);
						},
						function (e) { toast(e.message, "error"); self.benches = []; }
					);
				},
				get ownBenches() { return (this.benches || []).filter(function (b) { return b.own; }); },
				get sharedBenches() { return (this.benches || []).filter(function (b) { return !b.own; }); },
				get bench() {
					var name = this.form.bench;
					return (this.benches || []).find(function (b) { return b.name === name; }) || null;
				},
				get needsPlan() { return !!(this.bench && this.bench.shared); },
				selectBench: function (name) {
					this.form.bench = name;
					this.form.apps = [];
					if (this.needsPlan && !this.form.plan && this.plans.length) this.form.plan = this.plans[0].name;
				},
				toggleApp: function (app) {
					var i = this.form.apps.indexOf(app);
					if (i === -1) this.form.apps.push(app);
					else this.form.apps.splice(i, 1);
				},
				get preview() { return siteNamePreview(this.form.name, this.root); },
				create: function () {
					var self = this;
					if (!this.form.bench) return toast(__("Choose a bench"), "error");
					if (!this.form.name.trim()) return toast(__("Enter a subdomain"), "error");
					this.creating = true;
					api("create_site", {
						bench: this.form.bench,
						subdomain_or_name: this.form.name,
						apps: this.form.apps,
						plan: this.needsPlan ? this.form.plan : (this.form.plan || null),
					}).then(
						function (r) {
							self.created = r;
							self.form.name = "";
							self.form.apps = [];
							self.load();
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.creating = false; });
				},
			};
		});

		Alpine.data("nxSite", function () {
			return mixin({
				name: query("name"),
				loading: true,
				error: null,
				site: null,
				tab: (location.hash || "#apps").slice(1),
				busy: {},
				domain: "",
				withFiles: true,
				dropConfirm: "",
				timer: null,
				init: function () {
					var self = this;
					if (!this.name) {
						this.loading = false;
						this.error = __("No site selected");
						return;
					}
					this.load().then(function () { self.schedule(); });
					this.$watch("tab", function (t) { history.replaceState(null, "", "#" + t); });
				},
				destroy: function () { clearTimeout(this.timer); this.stopJobPoll(); },
				load: function (quiet) {
					var self = this;
					if (!quiet) this.loading = true;
					return api("get_site", { name: this.name }).then(
						function (r) { self.site = r; self.error = null; },
						function (e) { if (!quiet || !self.site) self.error = e.message; }
					).finally(function () { self.loading = false; });
				},
				get active() {
					var s = this.site;
					if (!s) return false;
					return ["Pending", "Moving"].indexOf(s.status) !== -1 || s.jobs.some(function (j) { return j.active; });
				},
				schedule: function () {
					var self = this;
					clearTimeout(this.timer);
					this.timer = setTimeout(function () {
						self.load(true).then(function () { self.schedule(); });
					}, this.active ? 4000 : 20000);
				},
				onJobFinished: function () { this.load(true); },
				run: function (action, args, confirmText) {
					var self = this;
					if (confirmText && !window.confirm(confirmText)) return Promise.resolve();
					this.busy[action] = true;
					return api("site_action", { name: this.name, action: action, args: args || {} }).then(
						function (r) {
							toast(__("Job queued: {0}", [__(ACTION_LABELS[action] || action)]), "success");
							return self.load(true).then(function () {
								self.schedule();
								if (r && r.job) {
									self.tab = "jobs";
									if (self.openJob !== r.job) self.toggleJob(r.job);
								}
							});
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.busy[action] = false; });
				},
				addDomain: function () {
					var d = this.domain.trim().toLowerCase();
					if (!d) return;
					var self = this;
					this.run("add_domain", { domain: d }).then(function () { self.domain = ""; });
				},
				drop: function () {
					if (this.dropConfirm !== this.site.name) return toast(__("Type the site name to confirm"), "error");
					this.run("drop", { skip_backup: 0 });
				},
			}, jobViewer());
		});

		Alpine.data("nxServers", function () {
			return {
				loading: true,
				error: null,
				servers: [],
				byos: null,
				adding: false,
				generating: false,
				token: null,
				reg: null,
				pollTimer: null,
				init: function () {
					this.load();
					if (query("add")) this.adding = true;
				},
				destroy: function () { clearTimeout(this.pollTimer); },
				load: function () {
					var self = this;
					return api("list_servers").then(
						function (r) { self.servers = r.servers; self.byos = r.byos_plan; self.error = null; },
						function (e) { self.error = e.message; }
					).finally(function () { self.loading = false; });
				},
				generate: function () {
					var self = this;
					this.generating = true;
					this.reg = null;
					api("create_registration_token").then(
						function (r) {
							self.token = r;
							if (r.byos_plan) self.byos = r.byos_plan;
							self.poll();
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.generating = false; });
				},
				poll: function () {
					var self = this;
					clearTimeout(this.pollTimer);
					if (!this.token) return;
					api("registration_status", { name: this.token.name }).then(
						function (r) {
							self.reg = r;
							if (r.server && r.server.status === "Active") {
								toast(__("Server {0} is connected", [r.server.hostname || r.server.name]), "success");
								self.load();
								return;
							}
							if (r.expired && !r.used) return;
							self.pollTimer = setTimeout(function () { self.poll(); }, 5000);
						},
						function () { self.pollTimer = setTimeout(function () { self.poll(); }, 10000); }
					);
				},
				get stage() {
					if (!this.token) return "start";
					var r = this.reg;
					if (!r || (!r.used && !r.expired)) return "waiting";
					if (r.expired && !r.used) return "expired";
					if (r.server && r.server.status === "Active") return "active";
					return "registered";
				},
			};
		});

		Alpine.data("nxServer", function () {
			return mixin({
				name: query("name"),
				loading: true,
				error: null,
				server: null,
				timer: null,
				init: function () {
					var self = this;
					if (!this.name) {
						this.loading = false;
						this.error = __("No server selected");
						return;
					}
					this.load();
					this.timer = setInterval(function () { self.load(true); }, 15000);
				},
				destroy: function () { clearInterval(this.timer); this.stopJobPoll(); },
				load: function (quiet) {
					var self = this;
					return api("get_server", { name: this.name }).then(
						function (r) { self.server = r; self.error = null; },
						function (e) { if (!quiet || !self.server) self.error = e.message; }
					).finally(function () { self.loading = false; });
				},
				get facts() { return this.server ? flatten(this.server.facts) : []; },
				onJobFinished: function () { this.load(true); },
			}, jobViewer());
		});

		Alpine.data("nxMarketplace", function () {
			return {
				loading: true,
				error: null,
				apps: [],
				categories: [],
				majors: ["14", "15", "16"],
				major: query("major") || "",
				category: query("category") || "",
				q: "",
				init: function () {
					var self = this;
					this.load();
					this.$watch("major", function () { self.load(); });
				},
				load: function () {
					var self = this;
					this.loading = true;
					return api("marketplace", { frappe_major: this.major || null }).then(
						function (r) {
							self.apps = r.apps;
							self.categories = r.categories;
							self.majors = r.majors;
							self.error = null;
						},
						function (e) { self.error = e.message; }
					).finally(function () { self.loading = false; });
				},
				get filtered() {
					var q = this.q.trim().toLowerCase();
					var cat = this.category;
					return this.apps.filter(function (a) {
						if (cat && a.category !== cat) return false;
						if (!q) return true;
						return [a.title, a.app_name, a.publisher, stripHtml(a.description || "")].join(" ").toLowerCase().indexOf(q) !== -1;
					});
				},
				majorsOf: function (app) {
					return (app.releases || []).map(function (r) { return "v" + r.frappe_major; });
				},
				plain: stripHtml,
			};
		});

		Alpine.data("nxBilling", function () {
			return {
				loading: true,
				unavailable: false,
				error: null,
				invoices: [],
				gateways: [],
				gateway: "",
				paying: null,
				init: function () { this.load(); },
				load: function () {
					var self = this;
					this.loading = true;
					return fetchInvoices().then(
						function (r) {
							self.invoices = invoiceList(r);
							self.unavailable = false;
							self.error = null;
							call("nexaploy.api.billing.get_payment_gateways").then(function (g) {
								self.gateways = Array.isArray(g) ? g : [];
								if (!self.gateway && self.gateways.length) self.gateway = self.gateways[0];
							}, function () { self.gateways = []; });
						},
						function (e) {
							if (isMissingMethod(e)) self.unavailable = true;
							else self.error = e.message;
						}
					).finally(function () { self.loading = false; });
				},
				amount: function (inv) { return money(invoiceAmount(inv), inv.currency); },
				unpaid: isUnpaid,
				period: function (inv) {
					return inv.period || [inv.period_start || inv.from_date, inv.period_end || inv.to_date].filter(Boolean).join(" – ");
				},
				pay: function (inv) {
					var self = this;
					if (!this.gateway) return toast(__("No payment method is available. Please contact support."), "error");
					this.paying = inv.name;
					call("nexaploy.api.billing.pay_invoice", { name: inv.name, gateway: this.gateway }).then(
						function (r) {
							var url = r && (r.url || r.payment_url || r.redirect_url || r.checkout_url);
							if (url) {
								window.location.href = url;
								return;
							}
							toast(__("Payment submitted"), "success");
							self.load();
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.paying = null; });
				},
			};
		});
	});
})();
