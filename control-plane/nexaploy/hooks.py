app_name = "nexaploy"
app_title = "Nexaploy"
app_publisher = "Nexaploy"
app_description = "Control plane for Nexaploy: Frappe/ERPNext hosting on our servers or yours (BYOS)"
app_email = "dev@nexaploy.local"
app_license = "mit"
required_apps = []

# Installation
# ------------
# Creates the "Nexaploy Customer" role and the default plans (idempotent).
after_install = "nexaploy.install.after_install"
after_migrate = "nexaploy.install.after_migrate"

# Fixtures
# --------
# The role is also exported as a fixture so `bench export-fixtures` keeps it in git.
fixtures = [
	{"dt": "Role", "filters": [["role_name", "in", ["Nexaploy Customer"]]]},
]

# Authentication
# --------------
# The agent authenticates with `Authorization: token <server_token>` (ARCHITECTURE.md §5.6).
# Frappe v15 raises AuthenticationError for any 2-part Authorization header that did not
# log a user in, *before* the whitelisted method runs. This hook verifies agent tokens for
# the nexaploy.api.agent.* endpoints only and sets the session user so the request reaches
# the method (which re-verifies the token itself).
auth_hooks = ["nexaploy.api.agent.validate_agent_auth"]

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
	"Deploy Candidate": "nexaploy.permissions.deploy_candidate_query",
	"Agent Job": "nexaploy.permissions.agent_job_query",
}

has_permission = {
	"Server": "nexaploy.permissions.has_team_permission",
	"Release Group": "nexaploy.permissions.has_team_permission",
	"Bench": "nexaploy.permissions.has_team_permission",
	"Site": "nexaploy.permissions.has_team_permission",
	"Site Backup": "nexaploy.permissions.has_team_permission",
	"Server Registration Token": "nexaploy.permissions.has_team_permission",
}

# Scheduled Tasks
# ---------------
scheduler_events = {
	"cron": {
		# mark servers Offline when no heartbeat for 5 minutes
		"* * * * *": ["nexaploy.tasks.mark_offline_servers"],
	},
	"hourly": ["nexaploy.tasks.prune_registration_tokens"],
}
