/* Nexaploy portal: "Restore to a point in time" card on /dashboard/site (Backups tab).
 *
 * Alpine.data("nxSitePitr") - nexaploy.api.pitr (ARCHITECTURE.md §11). Nested in nxSite, so
 * `site` and `load()` of the parent scope are visible. Served as /assets/nexaploy/js/pitr.js and
 * included as a plain <script> in the head of www/dashboard/site.html (before Alpine starts).
 *
 * The API speaks UTC ISO (`2026-11-04T15:15:00Z`); the picker (<input type="datetime-local">)
 * works in the browser's time zone and is limited to the restorable window.
 */
(function () {
	"use strict";

	var M = "nexaploy.api.pitr.";

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { return nx().toast(msg, kind); }
	function pad(n) { return n < 10 ? "0" + n : String(n); }

	/* Date -> "YYYY-MM-DDTHH:MM" (local time) for datetime-local inputs */
	function toLocalInput(d) {
		if (!d || isNaN(d.getTime())) return "";
		return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" +
			pad(d.getHours()) + ":" + pad(d.getMinutes());
	}

	/* datetime-local value (local time) -> UTC ISO "YYYY-MM-DDTHH:MM:SSZ" */
	function localToUtc(value) {
		if (!value) return null;
		var d = new Date(value);
		if (isNaN(d.getTime())) return null;
		return d.toISOString().replace(/\.\d{3}Z$/, "Z");
	}

	function factory() {
		return {
			pitr: null,
			pitrLoading: false,
			pitrError: null,
			pitrBusy: false,
			pitrAt: "",
			pitrMode: "copy",
			pitrConfirm: "",
			pitrResult: null,

			pitrSite: function () { return nx() ? nx().query("name") : new URLSearchParams(location.search).get("name"); },
			loadPitr: function () {
				var self = this;
				this.pitrLoading = true;
				return call("get_pitr_window", { site: this.pitrSite() }).then(
					function (r) {
						self.pitr = r;
						self.pitrError = null;
						if (r.available && !self.pitrAt) self.pitrAt = toLocalInput(new Date(r.end));
					},
					function (e) { self.pitrError = e.message; }
				).finally(function () { self.pitrLoading = false; });
			},
			/* minutes-precision bounds: start rounded up, end rounded down */
			pitrMin: function () {
				if (!this.pitr || !this.pitr.start) return "";
				var d = new Date(this.pitr.start);
				if (d.getSeconds() || d.getMilliseconds()) d = new Date(d.getTime() + 60000);
				return toLocalInput(d);
			},
			pitrMax: function () { return this.pitr && this.pitr.end ? toLocalInput(new Date(this.pitr.end)) : ""; },
			pitrInWindow: function () {
				var v = this.pitrAt;
				return !!v && v >= this.pitrMin() && v <= this.pitrMax();
			},
			pitrCanRestore: function () {
				if (!this.pitr || !this.pitr.available || !this.pitr.can_write || this.pitrBusy) return false;
				if (!this.pitrInWindow()) return false;
				if (this.pitrMode === "in_place") return this.pitrConfirm.trim().toLowerCase() === this.pitrSite();
				return true;
			},
			pitrUtc: function () { return localToUtc(this.pitrAt); },
			restorePitr: function () {
				var self = this;
				var at = this.pitrUtc();
				if (!at || !this.pitrCanRestore()) return;
				var when = nx().fmtDate(at);
				var text = this.pitrMode === "in_place"
					? t("Replace ALL current data of {0} with its state at {1}? A backup of the current state is taken first.", [this.pitrSite(), when])
					: t("Restore {0} as it was at {1} into a new site? The current site is not changed.", [this.pitrSite(), when]);
				if (!window.confirm(text)) return;
				this.pitrBusy = true;
				return call("restore_to_point", {
					site: this.pitrSite(), target_time: at, mode: this.pitrMode,
					confirm: this.pitrMode === "in_place" ? this.pitrConfirm.trim() : null,
				}).then(
					function (r) {
						self.pitrResult = r;
						self.pitrConfirm = "";
						toast(r.mode === "in_place"
							? t("Point-in-time restore started")
							: t("Restoring into {0}", [r.target_site]), "success");
						if (r.job) {
							nx().pollJob(r.job, function () {}).done.then(function (job) {
								if (job && job.status === "Failure") toast(t("Point-in-time restore failed. Check the jobs log."), "error");
								if (typeof self.load === "function") self.load(true);
							});
						}
						if (typeof self.load === "function") self.load(true);
					},
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.pitrBusy = false; });
			},
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxSitePitr", factory);
	});
})();
