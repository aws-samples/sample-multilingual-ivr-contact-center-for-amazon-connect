"""Case assignment and per-agent isolation.

Triggered by the Cases event stream (EventBridge):

- CASE.CREATED: fill Assigned Queue (default queue) and Assigned User (the case
  creator, from created_by) when empty. Cases created by a Lambda (voicemail)
  have no creator and stay unassigned in the shared voicemail pool.
- CASE.CREATED / CASE.UPDATED that changes assigned_user: set the case's
  `Owner` tag to the assigned agent's username. When unassigned, a voicemail
  case returns to the shared pool (`Owner=pool`); any other case loses the tag.

Agents hold a security profile restricted to `Owner=<their username>` on the
Case resource, so they only see cases assigned to them. Agents also hold a
pool profile (`Owner=pool`) so unclaimed voicemail cases are visible to all of
them. Untagged cases (history) are visible to the supervisor only.
"""
import os

import boto3

cases = boto3.client("connectcases")
connect = boto3.client("connect")

DOMAIN = os.environ["CASES_DOMAIN_ID"]
DEFAULT_QUEUE_ARN = os.environ["DEFAULT_QUEUE_ARN"]
VOICEMAIL_TEMPLATE_ID = os.environ.get("VOICEMAIL_TEMPLATE_ID", "")
OWNER_TAG = "Owner"
POOL_OWNER = "pool"


def _value(field):
    v = (field or {}).get("value") or {}
    return v.get("stringValue") or v.get("userArnValue") or ""


def _username(user_arn):
    # arn:aws:connect:<region>:<account>:instance/<instance-id>/agent/<user-id>
    parts = user_arn.split("/")
    instance_id, user_id = parts[-3], parts[-1]
    return connect.describe_user(InstanceId=instance_id, UserId=user_id)["User"]["Username"]


def _sync_owner_tag(case_arn, assigned_user, template_id):
    if assigned_user:
        cases.tag_resource(arn=case_arn, tags={OWNER_TAG: _username(assigned_user)})
    elif template_id and template_id == VOICEMAIL_TEMPLATE_ID:
        # Unclaimed voicemail goes back to the shared pool every agent can see.
        cases.tag_resource(arn=case_arn, tags={OWNER_TAG: POOL_OWNER})
    else:
        cases.untag_resource(arn=case_arn, tagKeys=[OWNER_TAG])


def handler(event, context):
    detail = event.get("detail", {})
    event_type = detail.get("eventType")
    case_id = (detail.get("case") or {}).get("caseId")
    if event_type not in ("CASE.CREATED", "CASE.UPDATED") or not case_id:
        return {"status": "skip", "reason": event_type}
    case_arn = next((r for r in event.get("resources", []) if "/case/" in r), None)

    got = cases.get_case(
        domainId=DOMAIN, caseId=case_id,
        fields=[{"id": "created_by"}, {"id": "assigned_user"}, {"id": "assigned_queue"}])
    cur = {f["id"]: _value(f) for f in got.get("fields", [])}
    template_id = got.get("templateId", "")

    if event_type == "CASE.CREATED":
        updates = []
        if not cur.get("assigned_queue"):
            updates.append({"id": "assigned_queue", "value": {"stringValue": DEFAULT_QUEUE_ARN}})
        if not cur.get("assigned_user") and cur.get("created_by"):
            updates.append({"id": "assigned_user", "value": {"stringValue": cur["created_by"]}})
            cur["assigned_user"] = cur["created_by"]
        if updates:
            cases.update_case(domainId=DOMAIN, caseId=case_id, fields=updates)
            print("case", case_id, "assigned", [u["id"] for u in updates])
        if cur.get("assigned_user") and case_arn:
            _sync_owner_tag(case_arn, cur["assigned_user"], template_id)
        return {"status": "ok", "case_id": case_id}

    # CASE.UPDATED: only act when the assignee changed.
    if "assigned_user" not in (detail.get("changedFieldIds") or []):
        return {"status": "skip", "reason": "assignee unchanged"}
    if case_arn:
        _sync_owner_tag(case_arn, cur.get("assigned_user"), template_id)
        print("case", case_id, "owner tag ->", cur.get("assigned_user") or "(supervisor only)")
    return {"status": "ok", "case_id": case_id}
