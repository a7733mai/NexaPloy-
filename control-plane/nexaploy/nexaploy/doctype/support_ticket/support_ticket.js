// Copyright (c) 2026, Nexaploy and contributors
// Staff reply (nexaploy.api.support.staff_reply): adds a staff reply, emails the team.

frappe.ui.form.on("Support Ticket", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.status !== "Closed") {
			frm.add_custom_button(__("Reply"), () => {
				frappe.prompt(
					[
						{ fieldname: "message", fieldtype: "Text Editor", label: __("Message"), reqd: 1 },
						{ fieldname: "resolve", fieldtype: "Check", label: __("Mark as Resolved") },
					],
					(v) =>
						frappe.call({
							method: "nexaploy.api.support.staff_reply",
							args: { name: frm.doc.name, message: v.message, status: v.resolve ? "Resolved" : null },
							freeze: true,
						}).then(() => frm.reload_doc()),
					__("Reply to customer"),
					__("Send")
				);
			}).addClass("btn-primary");
		}
		if (!frm.doc.assigned_to) {
			frm.add_custom_button(__("Assign to me"), () => frm.set_value("assigned_to", frappe.session.user).then(() => frm.save()));
		}
		if (frm.doc.first_response_due && !frm.doc.first_response_at) {
			const late = frappe.datetime.str_to_obj(frm.doc.first_response_due) < new Date();
			frm.dashboard.set_headline_alert(
				(late ? __("First response overdue since {0}") : __("First response due {0}")).replace("{0}", frappe.datetime.str_to_user(frm.doc.first_response_due)),
				late ? "red" : "orange"
			);
		}
	},
});
