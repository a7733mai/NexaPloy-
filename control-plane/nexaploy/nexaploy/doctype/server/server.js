// Copyright (c) 2026, Nexaploy and contributors
// Server: agent version vs the latest Agent Release of its channel, "Upgrade agent" and
// "Roll back agent" (nexaploy.api.agent_releases, logic in nexaploy/agent_upgrades.py).

frappe.ui.form.on("Server", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status === "Archived") return;
		const API = "nexaploy.api.agent_releases.";
		frappe.call({ method: API + "server_agent_status", args: { server: frm.doc.name } }).then((r) => {
			const st = r.message || {};
			if (st.latest) {
				const color = st.upgrade_available ? "orange" : "green";
				frm.dashboard.set_headline_alert(
					__("Agent {0} · latest {1} ({2})", [st.agent_version || "?", st.latest.version, __(st.channel)]),
					color
				);
			}
			if (!st.can_upgrade) return;
			if (st.upgrade_available && !st.blocker) {
				frm.add_custom_button(__("Upgrade agent to {0}", [st.latest.version]), () => {
					frappe.confirm(
						__("Upgrade the agent on {0} from {1} to {2}? Benches and sites keep running; the agent restarts.", [
							frm.doc.hostname || frm.doc.name, st.agent_version || "?", st.latest.version,
						]),
						() => frappe.call({ method: API + "upgrade_agent", args: { server: frm.doc.name }, freeze: true }).then(() => frm.reload_doc())
					);
				}, __("Agent"));
			}
			if (["Succeeded", "Failed"].includes(st.upgrade_status) && frm.doc.status === "Active") {
				frm.add_custom_button(__("Roll back agent"), () => {
					frappe.confirm(__("Put the previous agent version back on {0}?", [frm.doc.hostname || frm.doc.name]), () =>
						frappe.call({ method: API + "rollback_agent", args: { server: frm.doc.name }, freeze: true }).then(() => frm.reload_doc())
					);
				}, __("Agent"));
			}
		});
	},
});
