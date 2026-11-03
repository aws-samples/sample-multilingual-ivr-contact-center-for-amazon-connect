"""Starts VMX voicemail processing as soon as the recording lands in S3 (EventBridge Object Created).

By default VMX waits for the contact record to arrive through Kinesis, which can take minutes at low volume.
This function takes the contact ID from the recording key, reads the vmx3_* contact attributes, builds the
contact record structure RecordingProcessor expects, and invokes RecordingProcessor directly.
Only voicemail contacts (with vmx3_timestamp and vmx3_mode) are processed; other recordings are skipped.
"""
import os, json, base64, urllib.parse, logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)
_lambda = boto3.client("lambda")
_connect = boto3.client("connect")

INSTANCE_ID = os.environ["instance_id"]
RECORDING_PROCESSOR = os.environ["recording_processor"]


def handler(event, context):
    detail = event.get("detail", {})
    bucket = detail.get("bucket", {}).get("name")
    key = urllib.parse.unquote_plus(detail.get("object", {}).get("key", ""))
    if not bucket or "/ivr/" not in key or not key.endswith(".wav"):
        logger.info("skip (not ivr wav): %s", key)
        return "skip"

    contact_id = key.rsplit("/", 1)[-1].split("_", 1)[0]
    try:
        attrs = _connect.get_contact_attributes(
            InstanceId=INSTANCE_ID, InitialContactId=contact_id).get("Attributes", {})
    except Exception as e:  # unknown or invalid contact ID: skip without retrying
        logger.info("skip (attrs lookup failed) contact=%s: %s", contact_id, e)
        return "skip"

    # Voicemail only (answered calls have no vmx3_timestamp / vmx3_mode)
    if "vmx3_timestamp" not in attrs or "vmx3_mode" not in attrs:
        logger.info("skip (not a voicemail) contact=%s", contact_id)
        return "skip"

    vm_ts = attrs["vmx3_timestamp"]
    ctr_like = {
        "ContactId": contact_id,
        "Attributes": attrs,
        "Recordings": [{
            "ParticipantType": "IVR",
            "Location": bucket + "/" + key,
            "StartTimestamp": vm_ts,   # offset 0: the recording is the beep plus the message
        }],
    }
    payload = {"Records": [{"kinesis": {
        "data": base64.b64encode(json.dumps(ctr_like).encode()).decode()}}]}

    _lambda.invoke(FunctionName=RECORDING_PROCESSOR, InvocationType="Event",
                   Payload=json.dumps(payload).encode())
    logger.info("invoked RecordingProcessor for contact=%s key=%s", contact_id, key)
    return "invoked:" + contact_id
