/* Nexaploy portal: Hosted servers created on a cloud provider (Hetzner Cloud, DigitalOcean).
 *
 * Served as /assets/nexaploy/js/provisioning.js and loaded by www/dashboard/servers.html as a
 * plain <script> so it registers before Alpine starts. Registers the Alpine component
 * `nxHostedServer` and the store `nxHosted` ({available}). Talks to the servers list via
 * window events: listens to `nx-hosted-open` / `nx-hosted-delete` and fires
 * `nx-servers-reload`. API: nexaploy.api.provisioning.*
 */
(function () {
	"use strict";
	if (window.__nxProvisioningLoaded) return;
	window.__nxProvisioningLoaded = true;

	var API = "nexaploy.api.provisioning.";

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(API + method, args); }
	function toast(msg, kind) { return nx().toast(msg, kind); }
	function reload() { window.dispatchEvent(new CustomEvent("nx-servers-reload")); }

	var STEPS = ["Creating", "Bootstrapping", "Ready"];

	function register() {
		var Alpine = window.Alpine;
		Alpine.store("nxHosted", { available: false });

		Alpine.data("nxHostedServer", function () {
			return {
				open: false,
				loading: false,
				providers: [],
				canCreate: false,
				provider: null,
				region: null,
				size: null,
				title: "",
				creating: false,
				server: null,
				pollTimer: null,
				steps: STEPS,
				init: function () {
					var self = this;
					call("list_options").then(function (r) {
						self.providers = r.providers || [];
						self.canCreate = !!r.can_create;
						Alpine.store("nxHosted").available = self.canCreate && self.providers.length > 0;
						if (self.providers.length) self.pickProvider(self.providers[0].name);
						if (new URLSearchParams(location.search).get("hosted") && self.canCreate) self.open = true;
					}, function () { /* no options: the button stays hidden */ });
				},
				destroy: function () { clearTimeout(this.pollTimer); },
				get current() {
					var name = this.provider;
					return this.providers.filter(function (p) { return p.name === name; })[0] || null;
				},
				get selectedSize() {
					var cur = this.current, name = this.size;
					return cur ? cur.sizes.filter(function (s) { return s.name === name; })[0] || null : null;
				},
				pickProvider: function (name) {
					this.provider = name;
					var cur = this.current;
					this.region = cur && cur.regions.length ? cur.regions[0].name : null;
					this.size = cur && cur.sizes.length ? cur.sizes[0].name : null;
				},
				openForm: function () {
					this.open = true;
					this.server = null;
				},
				spec: function (s) {
					var parts = [];
					if (s.vcpu) parts.push(t("{0} vCPU", [s.vcpu]));
					if (s.memory_gb) parts.push(t("{0} GB RAM", [s.memory_gb]));
					if (s.disk_gb) parts.push(t("{0} GB disk", [s.disk_gb]));
					return parts.join(" · ");
				},
				create: function () {
					var self = this;
					if (!this.region || !this.size) return;
					this.creating = true;
					call("create_hosted_server", { region: this.region, size: this.size, title: this.title }).then(
						function (r) {
							self.server = r;
							toast(t("Server {0} is being created", [r.server_id]), "success");
							reload();
							self.poll();
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.creating = false; });
				},
				poll: function () {
					var self = this;
					clearTimeout(this.pollTimer);
					if (!this.server) return;
					call("provisioning_status", { server: this.server.name }).then(
						function (r) {
							self.server = r;
							if (r.provisioning_status === "Ready" || r.provisioning_status === "Failed") {
								reload();
								return;
							}
							self.pollTimer = setTimeout(function () { self.poll(); }, 10000);
						},
						function () { self.pollTimer = setTimeout(function () { self.poll(); }, 20000); }
					);
				},
				stepState: function (step) {
					var status = this.server && this.server.provisioning_status;
					if (status === "Failed") return "";
					var at = STEPS.indexOf(status), idx = STEPS.indexOf(step);
					if (idx < at || status === "Ready") return "done";
					return idx === at ? "current" : "";
				},
				remove: function (detail) {
					if (!detail || !detail.name) return;
					var label = detail.hostname || detail.name;
					if (!window.confirm(t("Delete server {0}? The virtual machine and all its data are destroyed.", [label]))) return;
					call("delete_hosted_server", { server: detail.name }).then(
						function () { toast(t("Server {0} is being deleted", [label]), "success"); reload(); },
						function (e) { toast(e.message, "error"); }
					);
				},
			};
		});
	}

	if (window.Alpine && window.Alpine.version) {
		register();
		document.querySelectorAll("[x-data='nxHostedServer']").forEach(function (el) {
			if (!el._x_dataStack) window.Alpine.initTree(el);
		});
	} else {
		document.addEventListener("alpine:init", register);
	}
})();
