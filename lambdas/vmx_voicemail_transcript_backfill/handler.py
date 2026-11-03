import os, json, boto3

cases = boto3.client("connectcases")
connect = boto3.client("connect")
s3 = boto3.client("s3")

DOMAIN = os.environ["CASES_DOMAIN_ID"]
F_VT = os.environ["F_VT"]
IID = os.environ["INSTANCE_ID"]

def _contact_id(key):
    # VMX transcript keys: YYYY/MM/DD/<contactId>.json
    return os.path.basename(key).split(".")[0]

def handler(event, context):
    detail = event.get("detail", {})
    bucket = detail.get("bucket", {}).get("name")
    key = detail.get("object", {}).get("key")
    if not bucket or not key:
        return {"status": "skip", "reason": "no key"}
    cid = _contact_id(key)
    try:
        attrs = connect.get_contact_attributes(InstanceId=IID, InitialContactId=cid).get("Attributes", {})
    except connect.exceptions.ResourceNotFoundException:
        print("contact not found (old/expired), skip", cid); return {"status": "skip", "reason": "contact not found"}
    case_id = attrs.get("vm_case_id")
    if not case_id:
        print("no vm_case_id for", cid, "- not a ticketed voicemail, skip")
        return {"status": "skip", "reason": "no vm_case_id"}
    obj = s3.get_object(Bucket=bucket, Key=key)
    data = json.loads(obj["Body"].read())
    transcript = data.get("results", {}).get("transcripts", [{}])[0].get("transcript", "")
    if not transcript:
        return {"status": "skip", "reason": "empty transcript"}
    cases.update_case(domainId=DOMAIN, caseId=case_id,
                      fields=[{"id": F_VT, "value": {"stringValue": transcript[:2000]}}])
    print("case", case_id, "updated with transcript, contact", cid)
    return {"status": "ok", "case_id": case_id}
