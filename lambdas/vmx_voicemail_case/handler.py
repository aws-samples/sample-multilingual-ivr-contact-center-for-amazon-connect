import os
import secrets

import boto3

cases = boto3.client("connectcases")
cp = boto3.client("customer-profiles")

DOMAIN = os.environ["CASES_DOMAIN_ID"]
TEMPLATE = os.environ["CASES_TEMPLATE_ID"]
PROFILES_DOMAIN = os.environ["PROFILES_DOMAIN"]
F_TICKET = os.environ["F_TICKET"]
F_TITLE = "title"
F_CUSTOMER = "customer_id"

def _unique_ticket():
    for _ in range(12):
        t = str(100000 + secrets.randbelow(900000))
        r = cases.search_cases(
            domainId=DOMAIN, maxResults=1,
            filter={"field": {"equalTo": {"id": F_TICKET, "value": {"stringValue": t}}}})
        if not r.get("cases"):
            return t
    return str(100000 + secrets.randbelow(900000))

def handler(event, context):
    cd = event.get("Details", {}).get("ContactData", {})
    attrs = cd.get("Attributes", {}) or {}
    caller = (cd.get("CustomerEndpoint", {}) or {}).get("Address") or attrs.get("vmx3_from", "") or "unknown"
    ticket = _unique_ticket()
    fields = [
        {"id": F_TITLE, "value": {"stringValue": "Voicemail - %s" % caller}},
        {"id": F_TICKET, "value": {"stringValue": ticket}},
    ]
    try:
        pid = attrs.get("vm_profile_id") or ""
        if not pid:
            pr = cp.search_profiles(DomainName=PROFILES_DOMAIN, KeyName="_phone", Values=[caller])
            items = pr.get("Items", [])
            if items:
                pid = items[0]["ProfileId"]
        if pid:
            region = os.environ.get("AWS_REGION", "us-east-1")
            account = context.invoked_function_arn.split(":")[4]
            profile_arn = "arn:aws:profile:%s:%s:domains/%s/profiles/%s" % (region, account, PROFILES_DOMAIN, pid)
            fields.append({"id": F_CUSTOMER, "value": {"stringValue": profile_arn}})
    except Exception as e:
        print("profile link skipped:", e)
    try:
        resp = cases.create_case(domainId=DOMAIN, templateId=TEMPLATE, fields=fields)
    except Exception as e:
        # Still create the case if linking the customer fails: retry without customer_id
        print("create_case with customer failed, retry without:", e)
        fields = [f for f in fields if f["id"] != F_CUSTOMER]
        resp = cases.create_case(domainId=DOMAIN, templateId=TEMPLATE, fields=fields)
    print("created case", resp["caseId"], "ticket", ticket, "caller", caller)
    return {"ticket_no": ticket, "ticket_spoken": ". ".join(ticket), "case_id": resp["caseId"]}
