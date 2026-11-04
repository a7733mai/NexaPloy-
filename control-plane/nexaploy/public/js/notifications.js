/* Nexaploy portal: notification channels (Email, Telegram, WhatsApp, Slack, Discord, webhook).
 *
 * Served as /assets/nexaploy/js/notifications.js and included as a plain <script> in the head
 * block of www/dashboard/notifications.html so Alpine.data("nxNotifications") registers before
 * Alpine starts. API: nexaploy.api.notifications.* (team Owners/Admins). Secrets are
 * write-only: the server only says whether one is stored (has_<field>).
 */
(function () {
	"use strict";

	var M = "nexaploy.api.notifications.";
	var TYPE_HINTS = {
		Email: "Leave recipients empty to email the team's Owners, Admins and Developers (billing events: the billing contacts).",
		Telegram: "Create a bot with @BotFather, paste its token, send any message to the bot (or add it to a group), then click Detect chat.",
		WhatsApp: "Uses the WhatsApp Business Cloud API. Without an approved template, messages only arrive within 24 hours of the recipient's last message.",
		Slack: "Create an incoming webhook in your Slack workspace and paste its URL.",
		Discord: "Channel settings → Integrations → Webhooks → New webhook → Copy webhook URL.",
		Webhook: "We POST JSON {event, title, message, context, url, team, timestamp}. With a signing secret, X-Nexaploy-Signature carries sha256=<hex HMAC-SHA256 of the raw body>.",
	};

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { if (nx()) nx().toast(msg, kind); }

	function blank(type, events) {
		return {
			name: null, title: "", channel_type: type || "Telegram", enabled: true, events: (events || []).slice(),
			email_recipients: "", telegram_bot_token: "", telegram_chat_id: "",
			whatsapp_phone_number_id: "", whatsapp_access_token: "", whatsapp_to: "", whatsapp_template: "", whatsapp_language: "en_US",
			webhook_url: "", webhook_secret: "",
		};
	}

	function factory() {
		return {
			loading: true,
			busy: false,
			error: null,
			opts: null,
			channels: [],
			form: null,
			stored: {},
			chats: [],
			detecting: false,
			testing: {},

			init: function () {
				if (nx().can("admin")) this.load();
				else this.loading = false;
			},

			load: function () {
				var self = this;
				this.loading = true;
				return Promise.all([this.opts ? Promise.resolve(this.opts) : call("options"), call("list_channels")]).then(function (r) {
					self.opts = r[0];
					self.channels = r[1].channels || [];
					self.error = null;
				}).catch(function (e) { self.error = e.message; }).finally(function () { self.loading = false; });
			},

			allEvents: function () { return this.opts ? this.opts.events.map(function (e) { return e.event; }) : []; },
			label: function (ev) {
				var row = this.opts && this.opts.events.filter(function (e) { return e.event === ev; })[0];
				return t(row ? row.label : ev);
			},
			hint: function (type) { return t(TYPE_HINTS[type] || ""); },
			eventsText: function (c) {
				var self = this;
				if (c.events.length === this.allEvents().length) return t("All events");
				return c.events.map(function (e) { return self.label(e); }).join(", ");
			},
			target: function (c) {
				if (c.channel_type === "Email") return c.email_recipients || t("Team members");
				if (c.channel_type === "Telegram") return c.telegram_chat_id;
				if (c.channel_type === "WhatsApp") return c.whatsapp_to ? "+" + c.whatsapp_to : "";
				return c.webhook_host || "";
			},
			statusText: function (c) {
				if (!c.enabled) return t("Disabled");
				if (c.failure_count) return t("Failing");
				if (c.last_sent_at) return t("Working");
				return t("Not used yet");
			},
			statusClass: function (c) {
				if (!c.enabled) return "badge";
				if (c.failure_count) return "badge danger";
				return c.last_sent_at ? "badge success" : "badge plain";
			},

			openNew: function (type) {
				this.form = blank(type, this.allEvents());
				this.stored = {};
				this.chats = [];
			},
			edit: function (c) {
				var f = blank(c.channel_type, c.events);
				Object.keys(f).forEach(function (k) {
					if (k in c && ["telegram_bot_token", "whatsapp_access_token", "webhook_url", "webhook_secret"].indexOf(k) === -1) f[k] = c[k] === null ? "" : c[k];
				});
				f.enabled = !!c.enabled;
				f.events = c.events.slice();
				this.form = f;
				this.stored = {
					telegram_bot_token: c.has_telegram_bot_token, whatsapp_access_token: c.has_whatsapp_access_token,
					webhook_url: c.has_webhook_url, webhook_secret: c.has_webhook_secret, bot_hint: c.telegram_bot_hint || "",
				};
				this.chats = [];
			},
			cancel: function () { this.form = null; this.chats = []; },
			placeholder: function (field) { return this.stored[field] ? t("Stored — leave empty to keep") : ""; },

			toggle: function (ev) {
				var i = this.form.events.indexOf(ev);
				if (i === -1) this.form.events.push(ev);
				else this.form.events.splice(i, 1);
			},
			setGroup: function (group, on) {
				var ev = this.form.events;
				group.events.forEach(function (e) {
					var i = ev.indexOf(e);
					if (on && i === -1) ev.push(e);
					if (!on && i !== -1) ev.splice(i, 1);
				});
			},

			save: function () {
				var self = this;
				var data = {};
				Object.keys(this.form).forEach(function (k) { if (k !== "name") data[k] = self.form[k]; });
				data.enabled = this.form.enabled ? 1 : 0;
				this.busy = true;
				var req = this.form.name
					? call("update_channel", { name: this.form.name, channel: data })
					: call("create_channel", { channel: data });
				return req.then(function (r) {
					if (r && r.email_channel) toast(t("An Email channel was added so your team keeps receiving emails. Disable it if you do not need it."), "info");
					else toast(t("Channel saved"), "success");
					self.form = null;
					return self.load();
				}).catch(function (e) { toast(e.message, "error"); }).finally(function () { self.busy = false; });
			},

			toggleEnabled: function (c) {
				var self = this;
				return call("update_channel", { name: c.name, channel: { enabled: c.enabled ? 0 : 1 } }).then(function (r) {
					Object.assign(c, r);
				}).catch(function (e) { toast(e.message, "error"); self.load(); });
			},

			remove: function (c) {
				var self = this;
				if (!window.confirm(t("Delete the channel {0}?", [c.title]))) return null;
				return call("delete_channel", { name: c.name }).then(function () {
					toast(t("Channel deleted"), "success");
					return self.load();
				}).catch(function (e) { toast(e.message, "error"); });
			},

			test: function (c) {
				var self = this;
				this.testing[c.name] = true;
				return call("send_test", { name: c.name }).then(function (r) {
					if (r.channel) Object.assign(c, r.channel);
					if (r.ok) toast(t("Test message sent"), "success");
					else toast(t("Sending failed: {0}", [r.error || ""]), "error");
				}).catch(function (e) { toast(e.message, "error"); }).finally(function () { self.testing[c.name] = false; });
			},

			detectChat: function () {
				var self = this;
				var args = {};
				if (this.form.telegram_bot_token) args.bot_token = this.form.telegram_bot_token;
				else if (this.form.name && this.stored.telegram_bot_token) args.name = this.form.name;
				else return toast(t("Enter the bot token first"), "error");
				this.detecting = true;
				return call("telegram_detect_chat", args).then(function (r) {
					self.chats = r.chats || [];
					if (!self.chats.length) toast(t("No chats yet. Send a message to your bot, then try again."), "info");
					else if (self.chats.length === 1) self.form.telegram_chat_id = self.chats[0].id;
				}).catch(function (e) { toast(e.message, "error"); }).finally(function () { self.detecting = false; });
			},
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxNotifications", factory);
	});
})();
