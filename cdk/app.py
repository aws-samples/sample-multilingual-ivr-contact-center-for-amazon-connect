#!/usr/bin/env python3
"""CDK app entry point. Reads deployment parameters from the context in cdk.json; override with -c key=value.

    instance_alias      Amazon Connect instance alias (globally unique; also the login URL and resource name prefix)
    stack_name          CloudFormation stack name
    region              Deployment Region
    agents              Agents: username / first_name / last_name
    supervisor          Supervisor: username / first_name / last_name
    claim_phone_number  Whether to claim a phone number and associate it with ivr-main (billed monthly)
    phone_country_code  Country code of the phone number (default US)
    owner_wait_seconds  Seconds a returning caller is offered to the case owner first (default 20)
    cases_labels        Display names of the Cases fields, layout and templates (missing keys use CASES_LABELS)
"""
import json

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from stacks.connect_ivr_stack import ConnectIvrStack

app = cdk.App()

CASES_LABELS = {
    "ticket_field": "工单号", "transcript_field": "留言转写", "layout": "标准工单布局",
    "summary_section": "概要", "details_section": "更多信息", "standard_template": "标准工单",
    "voicemail_template": "语音留言工单", "agent_template_prefix": "工单-",
}


def ctx(name, default=None):
    value = app.node.try_get_context(name)
    if value is None:
        return default
    # Lists and booleans passed with -c arrive as strings
    if isinstance(value, str) and value[:1] in "[{":
        return json.loads(value)
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


config = {
    "instance_alias": ctx("instance_alias"),
    "agents": ctx("agents"),
    "supervisor": ctx("supervisor"),
    "claim_phone_number": ctx("claim_phone_number", True),
    "phone_country_code": ctx("phone_country_code", "US"),
    "owner_wait_seconds": int(ctx("owner_wait_seconds", 20)),
    "cases_labels": dict(CASES_LABELS, **(ctx("cases_labels") or {})),
}
if not config["agents"] or not config["supervisor"]:
    raise SystemExit("agents and supervisor must be set in cdk.json")
alias = config["instance_alias"] or ""
if not alias or "<" in alias:
    raise SystemExit("Set instance_alias in cdk.json to a globally unique instance alias, or pass -c instance_alias=<alias>")

ConnectIvrStack(app, ctx("stack_name", "ConnectIvrStack"), config=config,
                env=cdk.Environment(region=ctx("region", "us-east-1")))
# Security baseline: synthesis fails on any unacknowledged AwsSolutions finding.
cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app, verbose=True))
app.synth()
