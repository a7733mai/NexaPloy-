"""Creating Agent Jobs. Every mutation of a server goes through here (ARCHITECTURE.md §8).

The agent picks Pending jobs up via `nexaploy.api.agent.poll` and reports back through
`nexaploy.api.agent.job_update`, which dispatches to nexaploy.agent_callbacks.
"""

import frappe
from frappe import _

# Job kinds the agent understands (§5.2). Kept in sync with the Agent Job.kind Select.
JOB_KINDS = (
	"image.build",
	"bench.create",
	"bench.start",
	"bench.stop",
	"bench.restart",
	"bench.update",
	"bench.remove",
	"site.create",
	"site.install_app",
	"site.uninstall_app",
	"site.add_domain",
	"site.remove_domain",
	"site.backup",
	"site.restore",
	"site.migrate",
	"site.set_config",
	"site.drop",
	# operations
	"server.configure_backups",
	"server.health",
	"backup.prune_remote",
	"backup.test_remote",
	# plan usage / site config
	"site.usage",
	"site.get_config",
	"site.update_config",
	# lifecycle (staging, rename)
	"site.clone",
	"site.rename",
	# observability (logs, analytics, slow queries, SQL, console)
	"site.logs",
	"site.analytics",
	"site.slow_queries",
	"site.sql",
	"site.console",
	# custom domains: certificate served per host (TLS health)
	"site.tls_status",
	# managed e-mail relay (default outgoing Email Account)
	"site.configure_email",
	# safe update (Site Update Test)
	"site.smoke_check",
	# point-in-time recovery
	"site.restore_pitr",
	"server.archive_binlogs",
)


def create_agent_job(server, kind, params, reference_doctype=None, reference_name=None):
	"""Insert a Pending Agent Job for `server` and return the doc.

	`params` must be JSON-serialisable and match the agent REST body for `kind`, plus the
	path parameters of that endpoint (e.g. `site`, `bench`) - see README "Job params".
	"""
	if kind not in JOB_KINDS:
		frappe.throw(_("Unknown agent job kind {0}").format(kind))
	if not server:
		frappe.throw(_("Agent job {0} needs a server").format(kind))
	status = frappe.db.get_value("Server", server, "status")
	if status is None:
		frappe.throw(_("Server {0} does not exist").format(server))
	if status == "Archived":
		frappe.throw(_("Server {0} is archived").format(server))

	job = frappe.get_doc(
		{
			"doctype": "Agent Job",
			"server": server,
			"kind": kind,
			"params": frappe.as_json(params or {}),
			"status": "Pending",
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		}
	)
	job.insert(ignore_permissions=True)
	return job
