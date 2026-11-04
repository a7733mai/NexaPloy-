app_name = "nexaploy"
app_title = "Nexaploy"
app_publisher = "Nexaploy"
app_description = "Control plane for Nexaploy: Frappe/ERPNext hosting on our servers or yours (BYOS)"
app_email = "dev@nexaploy.local"
app_license = "mit"
required_apps = []

# Public SaaS signup: /signup/<product slug>
website_route_rules = [{"from_route": "/signup/<product>", "to_route": "signup"}]

# Customer portal (www/dashboard)
# -------------------------------
role_home_page = {"Nexaploy Customer": "dashboard"}
portal_menu_items = [
	{"title": "Dashboard", "route": "/dashboard", "reference_doctype": "", "role": "Nexaploy Customer"},
]

# Installation
# ------------
# Creates the "Nexaploy Customer" role and the default plans (idempotent).
after_install = "nexaploy.install.after_install"
after_migrate = "nexaploy.install.after_migrate"

# Fixtures
# --------
# The role is also exported as a fixture so `bench export-fixtures` keeps it in git.
fixtures = [
	{"dt": "Role", "filters": [["role_name", "in", ["Nexaploy Customer", "Nexaploy 2FA", "Nexaploy Support"]]]},
]

# Authentication
# --------------
# The agent authenticates with `Authorization: token <server_token>` (ARCHITECTURE.md §5.6).
# Frappe v15 raises AuthenticationError for any 2-part Authorization header that did not
# log a user in, *before* the whitelisted method runs. This hook verifies agent tokens for
# the nexaploy.api.agent.* endpoints only and sets the session user so the request reaches
# the method (which re-verifies the token itself).
auth_hooks = ["nexaploy.api.agent.validate_agent_auth"]

# White-label: brand every web page (login, signup, portal) by request host
update_website_context = ["nexaploy.branding.update_website_context"]

# Permissions
# -----------
# Customers ("Nexaploy Customer") only see documents whose `team` is their user.
permission_query_conditions = {
	"Server": "nexaploy.permissions.server_query",
	"Release Group": "nexaploy.permissions.team_query",
	"Bench": "nexaploy.permissions.team_query",
	"Site": "nexaploy.permissions.team_query",
	"Site Backup": "nexaploy.permissions.team_query",
	"Server Registration Token": "nexaploy.permissions.team_query",
	"Nexaploy Invoice": "nexaploy.permissions.team_query",
	"Nexaploy Subscription": "nexaploy.permissions.team_query",
	"Nexaploy Payment": "nexaploy.permissions.team_query",
	"Site Usage": "nexaploy.permissions.team_query",
	"Custom App": "nexaploy.permissions.team_query",
	"GitHub Installation": "nexaploy.permissions.team_query",
	"Backup Destination": "nexaploy.permissions.team_query",
	"Team Activity": "nexaploy.permissions.team_activity_query",
	"Team Credit Transaction": "nexaploy.permissions.team_credit_query",
	"Marketplace Publisher": "nexaploy.api.publisher.publisher_query",
	"Publisher Payout": "nexaploy.billing.payouts.payout_query",
	"Support Ticket": "nexaploy.support.support_ticket_query",
	"Team": "nexaploy.permissions.team_doc_query",
	"Team Invitation": "nexaploy.permissions.team_invitation_query",
	"Deploy Candidate": "nexaploy.permissions.deploy_candidate_query",
	"Agent Job": "nexaploy.permissions.agent_job_query",
	"Notification Channel": "nexaploy.permissions.notification_channel_query",
	# managed e-mail / white-label brands
	"Site Email": "nexaploy.permissions.team_query",
	"Brand": "nexaploy.permissions.team_query",
	"Site Update Test": "nexaploy.permissions.team_query",
	"Console Audit Log": "nexaploy.console_access.console_audit_query",
}

has_permission = {
	"Server": "nexaploy.permissions.has_team_permission",
	"Release Group": "nexaploy.permissions.has_team_permission",
	"Bench": "nexaploy.permissions.has_team_permission",
	"Site": "nexaploy.permissions.has_team_permission",
	"Site Backup": "nexaploy.permissions.has_team_permission",
	"Server Registration Token": "nexaploy.permissions.has_team_permission",
	"Nexaploy Invoice": "nexaploy.permissions.has_team_permission",
	"Nexaploy Subscription": "nexaploy.permissions.has_team_permission",
	"Nexaploy Payment": "nexaploy.permissions.has_team_permission",
	"Site Usage": "nexaploy.permissions.has_team_permission",
	"Custom App": "nexaploy.permissions.has_team_permission",
	"GitHub Installation": "nexaploy.permissions.has_team_permission",
	"Backup Destination": "nexaploy.permissions.has_team_permission",
	"Team Activity": "nexaploy.permissions.has_read_only_team_permission",
	"Team Credit Transaction": "nexaploy.permissions.has_read_only_team_permission",
	"Marketplace Publisher": "nexaploy.permissions.has_team_permission",
	"Publisher Payout": "nexaploy.permissions.has_read_only_team_permission",
	"Support Ticket": "nexaploy.support.has_support_ticket_permission",
	"Team": "nexaploy.permissions.has_team_doc_permission",
	"Team Invitation": "nexaploy.permissions.has_team_permission",
	"Notification Channel": "nexaploy.permissions.has_notification_channel_permission",
	"Site Email": "nexaploy.permissions.has_team_permission",
	"Brand": "nexaploy.permissions.has_team_permission",
	"Site Update Test": "nexaploy.permissions.has_team_permission",
	"Console Audit Log": "nexaploy.permissions.has_read_only_team_permission",
	# SQL / console / log jobs: only with the console capability that ran them
	"Agent Job": "nexaploy.console_access.has_agent_job_permission",
}

# Document Events
# ---------------
# Billing: keep subscriptions in sync with servers/sites (and paid apps on sites).
doc_events = {
	"Server": {
		"after_insert": "nexaploy.billing.subscriptions.sync_subscription",
		"on_update": "nexaploy.billing.subscriptions.sync_subscription",
		"on_trash": "nexaploy.billing.subscriptions.cancel_document_subscriptions",
	},
	"Site": {
		"after_insert": "nexaploy.billing.subscriptions.sync_subscription",
		"on_update": "nexaploy.billing.subscriptions.sync_subscription",
		"on_trash": "nexaploy.billing.subscriptions.cancel_document_subscriptions",
	},
}

# Scheduled Tasks
# ---------------
scheduler_events = {
	"cron": {
		# mark servers Offline when no heartbeat for 5 minutes
		"* * * * *": ["nexaploy.tasks.mark_offline_servers"],
		"*/5 * * * *": [
			"nexaploy.tasks.check_alerts",
			"nexaploy.tasks.check_sites_uptime",
			"nexaploy.provisioning.jobs.check_provisioning",
			"nexaploy.agent_callbacks.retry_failed_callbacks",
			"nexaploy.domains.verify_pending_domains",
			"nexaploy.update_test.sync_update_tests",
			"nexaploy.agent_upgrades.check_agent_upgrades",
			# offsite attachments (§15): replication per Storage Settings interval, remount moved sites
			"nexaploy.storage.schedule_replication",
		],
		"*/15 * * * *": [
			"nexaploy.tasks.apply_auto_updates",
			"nexaploy.tasks.enforce_plan_limits",
			"nexaploy.pitr.schedule_binlog_archiving",
		],
	},
	"hourly": [
		"nexaploy.tasks.prune_registration_tokens",
		"nexaploy.tasks.schedule_backups",
		"nexaploy.support_tasks.auto_close_resolved_tickets",
	],
	"daily": [
		"nexaploy.tasks.prune_remote_backups",
		"nexaploy.tasks.prune_server_metrics",
		"nexaploy.tasks.prune_site_usage",
		"nexaploy.billing.subscriptions.reconcile_subscriptions",
		"nexaploy.billing.invoicing.generate_monthly_invoices",
		"nexaploy.billing.invoicing.mark_overdue_and_suspend",
		"nexaploy.billing.payouts.generate_publisher_payouts",
		"nexaploy.saas.expire_trials",
		"nexaploy.all_in_one.backup_control_plane",
		"nexaploy.email_relay.recheck_domains",
		"nexaploy.storage.prune_trash",
	],
}
