// Copyright (c) 2026, Nexaploy and contributors
// Only offer marketplace apps with a verified release for the group's Frappe major
// (the server re-checks on save: nexaploy.utils.check_group_apps + required apps).

frappe.ui.form.on("Release Group", {
	setup(frm) {
		frm.set_query("app", "apps", () => ({
			query: "nexaploy.api.marketplace.app_query",
			filters: { frappe_major: frm.doc.frappe_major || "" },
		}));
	},
});
