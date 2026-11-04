// Copyright (c) 2026, Nexaploy and contributors
// Review of publisher listings (nexaploy.api.publisher.approve_listing / reject_listing / unpublish_listing).

frappe.ui.form.on("Marketplace App", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;
		const review = (method, label, needNotes) => {
			const run = (notes) =>
				frappe.call({
					method: "nexaploy.api.publisher." + method,
					args: { app: frm.doc.name, notes: notes || null },
					freeze: true,
				}).then(() => frm.reload_doc());
			if (!needNotes) {
				frappe.confirm(__("{0} {1}?", [label, frm.doc.title]), () => run(frm.doc.review_notes));
				return;
			}
			frappe.prompt(
				[{ fieldname: "notes", fieldtype: "Small Text", label: __("Review Notes"), reqd: method === "reject_listing", default: frm.doc.review_notes }],
				(v) => run(v.notes),
				label
			);
		};
		if (frm.doc.status === "In Review") {
			frm.add_custom_button(__("Approve"), () => review("approve_listing", __("Approve"), true), __("Review"));
			frm.add_custom_button(__("Reject"), () => review("reject_listing", __("Reject"), true), __("Review"));
		}
		if (frm.doc.status === "Published" && frm.doc.publisher_profile) {
			frm.add_custom_button(__("Unpublish"), () => review("unpublish_listing", __("Unpublish"), true), __("Review"));
		}
		if (frm.doc.source_custom_app) {
			frm.set_intro(__("Listed by {0} from custom app {1}. Check that the repository is public before approving: marketplace builds clone it without credentials.", [frm.doc.publisher || frm.doc.publisher_profile, frm.doc.source_custom_app]));
		}
	},
});
