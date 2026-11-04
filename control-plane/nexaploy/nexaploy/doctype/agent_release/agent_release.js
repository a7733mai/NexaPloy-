// Copyright (c) 2026, Nexaploy and contributors
// Bulk agent upgrade (nexaploy.api.agent_releases.bulk_upgrade_agents): platform admins only.

frappe.ui.form.on("Agent Release", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.published || !frappe.user.has_role("System Manager")) return;
		frm.add_custom_button(__("Upgrade outdated servers"), () => {
			const method = "nexaploy.api.agent_releases.bulk_upgrade_agents";
			frappe.call({ method, args: { dry_run: 1 } }).then((r) => {
				const rows = (r.message && r.message.servers) || [];
				if (!rows.length) {
					frappe.msgprint(__("Every active server already runs the latest agent of its channel."));
					return;
				}
				const esc = frappe.utils.escape_html;
				const list = rows.map((s) => `${esc(s.hostname || s.server)}: ${esc(s.from || "?")} → ${esc(s.to)}`).join("<br>");
				frappe.confirm(__("Upgrade the agent on {0} servers?", [rows.length]) + "<br><br>" + list, () => {
					frappe.call({ method, args: { dry_run: 0 }, freeze: true }).then((res) => {
						const done = ((res.message && res.message.servers) || []).filter((s) => s.job).length;
						frappe.show_alert({ message: __("Agent upgrade queued on {0} servers", [done]), indicator: "green" });
					});
				});
			});
		});
	},
});
