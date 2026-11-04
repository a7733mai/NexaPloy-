// Copyright (c) 2026, Nexaploy and contributors
// Draft -> Approved -> Paid (Credits payouts are credited to the publisher team when Paid).

frappe.ui.form.on("Publisher Payout", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;
		const setStatus = (status, payment_reference) =>
			frm.call("set_status", { status, payment_reference: payment_reference || null }).then(() => frm.reload_doc());
		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Approve"), () => setStatus("Approved")).addClass("btn-primary");
		}
		if (frm.doc.status === "Approved") {
			frm.add_custom_button(__("Back to Draft"), () => setStatus("Draft"));
			frm.add_custom_button(__("Mark Paid"), () => {
				if (frm.doc.payout_method === "Credits") {
					frappe.confirm(__("Add {0} to the publisher team's credits?", [format_currency(frm.doc.net, frm.doc.currency)]), () => setStatus("Paid"));
				} else {
					frappe.prompt([{ fieldname: "ref", fieldtype: "Data", label: __("Transfer Reference"), reqd: 1 }], (v) => setStatus("Paid", v.ref), __("Mark Paid"));
				}
			}).addClass("btn-primary");
		}
	},
});
