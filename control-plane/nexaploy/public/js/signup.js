/* Nexaploy public SaaS signup page (/signup/<product>, www/signup.html).
 *
 * Plain JS, no framework. APIs: nexaploy.api.signup.{check_subdomain, signup, verify_email,
 * signup_status}. The progress token returned by signup() is remembered in localStorage
 * (per product) so a reload keeps showing the progress.
 */
(function () {
	"use strict";
	var BOOT = window.NX_SIGNUP || {};
	var API = "nexaploy.api.signup.";
	var MESSAGES = BOOT.messages || {};
	var KEY = "nx_signup_" + (BOOT.product || "");
	var pollTimer = null;

	function t(msg, args) {
		var out = MESSAGES[msg] || msg;
		(args || []).forEach(function (a, i) { out = out.split("{" + i + "}").join(a); });
		return out;
	}

	function $(id) { return document.getElementById(id); }

	function store(token) {
		try {
			if (token) window.localStorage.setItem(KEY, token);
			else window.localStorage.removeItem(KEY);
		} catch (e) { /* private mode */ }
	}

	function stored() {
		try { return window.localStorage.getItem(KEY); } catch (e) { return null; }
	}

	function message(data) {
		if (data && data._server_messages) {
			try {
				var msgs = JSON.parse(data._server_messages).map(function (m) {
					try { return JSON.parse(m).message; } catch (e) { return m; }
				});
				if (msgs.length) return msgs.join(" ").replace(/<[^>]+>/g, "");
			} catch (e) { /* ignore */ }
		}
		return (data && (data.message || data.exception)) || null;
	}

	function call(method, args, httpMethod) {
		var headers = { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" };
		var opts = { method: httpMethod || "POST", headers: headers, credentials: "same-origin" };
		var url = "/api/method/" + API + method;
		if (opts.method === "GET") {
			url += "?" + new URLSearchParams(args || {}).toString();
		} else {
			headers["Content-Type"] = "application/json";
			if (window.frappe && window.frappe.csrf_token) headers["X-Frappe-CSRF-Token"] = window.frappe.csrf_token;
			opts.body = JSON.stringify(args || {});
		}
		return fetch(url, opts).then(function (res) {
			return res.json().catch(function () { return {}; }).then(function (data) {
				if (!res.ok || data.exc_type) {
					var msg = res.status === 429 ? t("Too many attempts. Please try again later.") : message(data);
					throw new Error(msg || t("Request failed ({0})", [res.status]));
				}
				return data.message;
			});
		}, function () { throw new Error(t("Network error. Check your connection.")); });
	}

	/* ---------------------------------------------------------------- availability */
	var checkTimer = null;
	var lastChecked = null;

	function checkSubdomain(value) {
		var box = $("nx-subdomain-status");
		clearTimeout(checkTimer);
		box.className = "small avail";
		box.textContent = "";
		if (!value || value.length < 3) return;
		checkTimer = setTimeout(function () {
			lastChecked = value;
			box.textContent = t("Checking…");
			call("check_subdomain", { product: BOOT.product, subdomain: value }, "GET").then(function (r) {
				if (lastChecked !== value) return;
				if (r.available) {
					box.className = "small avail ok";
					box.textContent = t("{0} is available", [r.site_name]);
				} else {
					box.className = "small avail bad";
					box.textContent = r.reason || t("This subdomain is already taken");
				}
			}, function (e) {
				box.className = "small avail bad";
				box.textContent = e.message;
			});
		}, 350);
	}

	/* ---------------------------------------------------------------- progress */
	function show(status) {
		$("nx-signup-form-card").hidden = !!status;
		$("nx-signup-progress").hidden = !status;
		var s = status && status.status;
		$("nx-step-verify").hidden = s !== "Pending Verification";
		$("nx-step-provisioning").hidden = !(s === "Verified" || s === "Provisioning");
		$("nx-step-active").hidden = s !== "Active";
		$("nx-step-failed").hidden = s !== "Failed";
		if (!status) return;
		$("nx-signup-email").textContent = status.email || "";
		$("nx-signup-site").textContent = status.site_name || "";
		if (s === "Active") {
			$("nx-signup-url").href = status.url;
			$("nx-signup-url").textContent = status.url;
		}
		if (s === "Failed") {
			$("nx-step-failed").textContent = status.error || t("Your site could not be created.");
		}
	}

	function poll(token) {
		clearTimeout(pollTimer);
		call("signup_status", { token: token }, "GET").then(function (r) {
			show(r);
			if (r.status === "Active" || r.status === "Failed") {
				store(null);
				return;
			}
			pollTimer = setTimeout(function () { poll(token); }, r.status === "Pending Verification" ? 10000 : 5000);
		}, function (e) {
			if (/unknown signup/i.test(e.message)) { store(null); show(null); return; }
			pollTimer = setTimeout(function () { poll(token); }, 15000);
		});
	}

	/* ---------------------------------------------------------------- submit */
	function onSubmit(ev) {
		ev.preventDefault();
		var form = ev.target;
		var err = $("nx-signup-error");
		var button = $("nx-signup-submit");
		err.hidden = true;
		var data = {
			product: BOOT.product,
			full_name: form.full_name.value.trim(),
			email: form.email.value.trim(),
			company: form.company.value.trim(),
			subdomain: form.subdomain.value.trim(),
			password: form.password.value,
		};
		if (!data.full_name || !data.email || !data.subdomain || !data.password) {
			err.textContent = t("Please fill in all required fields.");
			err.hidden = false;
			return;
		}
		button.disabled = true;
		call("signup", data).then(function (r) {
			store(r.token);
			form.password.value = "";
			show({ status: r.status, email: r.email, site_name: r.site_name });
			poll(r.token);
		}, function (e) {
			err.textContent = e.message;
			err.hidden = false;
		}).finally(function () { button.disabled = false; });
	}

	function init() {
		var form = $("nx-signup-form");
		if (!form) return;
		var login = $("nx-login-link");
		if (login) login.href = "/login?redirect-to=" + encodeURIComponent(location.pathname);
		if (BOOT.user && form.email && !form.email.value) form.email.value = BOOT.user.indexOf("@") > 0 ? BOOT.user : "";
		form.addEventListener("submit", onSubmit);
		form.subdomain.addEventListener("input", function () { checkSubdomain(form.subdomain.value.trim()); });

		if (BOOT.verify) {
			show({ status: "Provisioning" });
			call("verify_email", { token: BOOT.verify }).then(function (r) {
				show(r);
				if (r.status !== "Active" && r.status !== "Failed") poll(BOOT.verify);
			}, function (e) {
				show({ status: "Failed", error: e.message });
			});
			return;
		}
		var token = stored();
		if (token) poll(token);
	}

	if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
	else init();
})();
