"""Returning-caller lookup, invoked from ivr-main before the queue transfer.

Finds the caller's most recently updated open case (by the customer profile the
flow associated, `vm_profile_id`) and returns, for the flow:

- open_case_ref / open_case_owner / case_notice: shown to whichever agent takes
  the call, so they know an open case exists and who owns it.
- route_to_owner: "true" when the owner's status is Available (idle, on a call
  or in after-contact work); the flow then applies
  RoutingCriteria, which offers the contact to the owner first and falls back to
  any agent in the queue after OWNER_WAIT_SECONDS.

Invoked with response validation JSON (RoutingCriteria is an object).
"""
import os

import boto3

cases = boto3.client("connectcases")
connect = boto3.client("connect")

DOMAIN = os.environ["CASES_DOMAIN_ID"]
PROFILES_DOMAIN = os.environ["PROFILES_DOMAIN"]
OWNER_WAIT_SECONDS = int(os.environ.get("OWNER_WAIT_SECONDS", "20"))

NO_CASE = {"has_open_case": "false", "route_to_owner": "false",
           "open_case_ref": "", "open_case_owner": "", "case_notice": ""}


def _value(field):
    v = (field or {}).get("value") or {}
    return v.get("stringValue") or v.get("userArnValue") or ""


def _owner_available(instance_id, user_id):
    """True when the owner is in the Available status, even if they are on a
    call or in after-contact work right now: they may free up within the wait.
    Offline or any other status (break, lunch) skips the wait."""
    data = connect.get_current_user_data(
        InstanceId=instance_id, Filters={"Agents": [user_id]}).get("UserDataList", [])
    if not data:
        return False
    return (data[0].get("Status") or {}).get("StatusName", "") == "Available"


def handler(event, context):
    contact = event.get("Details", {}).get("ContactData", {})
    profile_id = (contact.get("Attributes") or {}).get("vm_profile_id", "")
    if not profile_id:
        return NO_CASE
    region, account = context.invoked_function_arn.split(":")[3:5]
    instance_id = contact.get("InstanceARN", "").split("/")[-1]
    profile_arn = "arn:aws:profile:%s:%s:domains/%s/profiles/%s" % (
        region, account, PROFILES_DOMAIN, profile_id)

    found = cases.search_cases(
        domainId=DOMAIN, maxResults=10,
        filter={"andAll": [
            {"field": {"equalTo": {"id": "customer_id", "value": {"stringValue": profile_arn}}}},
            {"field": {"equalTo": {"id": "status", "value": {"stringValue": "open"}}}},
        ]},
        sorts=[{"fieldId": "last_updated_datetime", "sortOrder": "Desc"}],
        fields=[{"id": "reference_number"}, {"id": "assigned_user"}],
    ).get("cases", [])
    if not found:
        return NO_CASE

    rows = [{f["id"]: _value(f) for f in c.get("fields", [])} for c in found]
    owned = next((r for r in rows if r.get("assigned_user")), None)
    if not owned:
        ref = rows[0].get("reference_number", "")
        return dict(NO_CASE, has_open_case="true", open_case_ref=ref,
                    open_case_owner="unassigned", case_notice="Open case %s (unassigned)" % ref)

    ref = owned.get("reference_number", "")
    user_id = owned["assigned_user"].split("/")[-1]
    username = connect.describe_user(InstanceId=instance_id, UserId=user_id)["User"]["Username"]
    result = {"has_open_case": "true", "open_case_ref": ref, "open_case_owner": username,
              "case_notice": "Open case %s, owner %s" % (ref, username),
              "route_to_owner": "false"}
    if _owner_available(instance_id, user_id):
        result["route_to_owner"] = "true"
        result["RoutingCriteria"] = {"Steps": [{
            "Expression": {"AttributeCondition": {
                "ComparisonOperator": "Match",
                "MatchCriteria": {"AgentsCriteria": {"AgentIds": [user_id]}}}},
            "Expiry": {"DurationInSeconds": OWNER_WAIT_SECONDS},
        }]}
    print("open case", ref, "owner", username, "route_to_owner", result["route_to_owner"])
    return result
