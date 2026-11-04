"""Scale fixes from the load test (e2e/loadtest): Agent Job indexes, job retention,
hourly metric roll-up."""

import ast
import json
import os
from datetime import datetime, timedelta

from nexaploy import ops_utils as ou

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nexaploy")
NOW = datetime(2026, 11, 5, 12, 0)


def test_agent_job_composite_indexes():
	src = open(os.path.join(APP, "nexaploy", "doctype", "agent_job", "agent_job.py")).read()
	tree = ast.parse(src)
	assert any(isinstance(n, ast.FunctionDef) and n.name == "on_doctype_update" for n in tree.body)
	assert '("server", "status")' in src and '("reference_doctype", "reference_name")' in src
	assert '("status", "modified")' in src


def test_job_retention_cutoffs_defaults_and_overrides():
	normal, house = ou.job_retention_cutoffs(NOW)
	assert normal == NOW - timedelta(days=90) and house == NOW - timedelta(days=7)
	normal, house = ou.job_retention_cutoffs(NOW, 30, 3)
	assert normal == NOW - timedelta(days=30) and house == NOW - timedelta(days=3)
	# nonsense or zero -> defaults; housekeeping never kept longer than everything else
	assert ou.job_retention_cutoffs(NOW, 0, "x") == (NOW - timedelta(days=90), NOW - timedelta(days=7))
	assert ou.job_retention_cutoffs(NOW, 5, 30)[1] == NOW - timedelta(days=5)


def test_housekeeping_kinds_are_real_job_kinds():
	tree = ast.parse(open(os.path.join(APP, "agent_job.py")).read())
	kinds = next(ast.literal_eval(n.value) for n in tree.body
	             if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "JOB_KINDS")
	assert ou.HOUSEKEEPING_JOB_KINDS <= set(kinds)
	# user-facing work is never on the short retention
	assert not {"site.create", "site.backup", "site.restore", "bench.update", "server.os_update"} & ou.HOUSEKEEPING_JOB_KINDS


def test_prune_and_rollup_are_scheduled_and_safe():
	hooks = open(os.path.join(APP, "hooks.py")).read()
	assert "nexaploy.tasks.prune_agent_jobs" in hooks and "nexaploy.tasks.prune_server_metrics" in hooks
	tasks = open(os.path.join(APP, "tasks.py")).read()
	body = tasks.split("def prune_agent_jobs", 1)[1].split("\ndef ", 1)[0]
	assert "FINISHED_JOB_STATUSES" in body and "Agent Job Step" in body and "limit=batch" in body
	roll = tasks.split("def rollup_server_metrics", 1)[1].split("\ndef ", 1)[0]
	assert "on duplicate key update" in roll and "ifnull(resolution, 'raw') = 'raw'" in roll
	metric = json.load(open(os.path.join(APP, "nexaploy", "doctype", "server_metric", "server_metric.json")))
	res = next(f for f in metric["fields"] if f["fieldname"] == "resolution")
	assert res["options"].split("\n") == ["raw", "hour"] and res["default"] == "raw"
