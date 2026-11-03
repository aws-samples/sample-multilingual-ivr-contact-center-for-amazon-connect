"""CloudFormation custom resource for deployment steps CloudFormation has no resource type for.

Operations (ResourceProperties.Operation):

- LookupFlows: return the IDs of contact flows and flow modules by exact name. Used for
  resources created by the VMX nested stack (no stack outputs) and for the instance's
  default "Default agent transfer" flow.
    Properties: InstanceId, Flows {key: flow name}, Modules {key: module name}
    Data:       {key: id} for every requested key
- RingbackPrompt: generate the China-standard ringback tone (450 Hz, 1 s on / 4 s off, 30 s,
  8 kHz 16-bit mono PCM WAV, as required by Amazon Connect prompts) and upload it to S3.
    Properties: Bucket, Key
    Data:       {S3Uri}
- EmptyBuckets: on stack deletion, delete every object (and object version) in the given
  buckets so CloudFormation can delete them. Used for the VMX buckets, which the VMX
  template creates without automatic object deletion.
    Properties: Buckets [bucket name, ...]

Invoked through the CDK Provider framework: return a dict with PhysicalResourceId and Data.
"""
import io
import math
import struct
import wave

import boto3

connect = boto3.client("connect")
s3 = boto3.client("s3")


def _all(paginator_name, key, **kw):
    out = []
    for page in connect.get_paginator(paginator_name).paginate(**kw):
        out.extend(page[key])
    return out


def _lookup_flows(props):
    instance_id = props["InstanceId"]
    data = {}
    wanted = props.get("Flows") or {}
    if wanted:
        by_name = {f["Name"]: f["Id"] for f in _all("list_contact_flows", "ContactFlowSummaryList",
                                                    InstanceId=instance_id)}
        for k, name in wanted.items():
            if name not in by_name:
                raise RuntimeError("contact flow not found: %s" % name)
            data[k] = by_name[name]
    wanted = props.get("Modules") or {}
    if wanted:
        by_name = {m["Name"]: m["Id"] for m in _all("list_contact_flow_modules",
                                                    "ContactFlowModulesSummaryList",
                                                    InstanceId=instance_id)}
        for k, name in wanted.items():
            if name not in by_name:
                raise RuntimeError("contact flow module not found: %s" % name)
            data[k] = by_name[name]
    return "lookup-" + instance_id, data


def _ringback_wav(sr=8000, freq=450.0, on=1.0, off=4.0, total=30.0, edge=0.02):
    frames = bytearray()
    for i in range(int(total * sr)):
        sec = i / sr
        phase = sec % (on + off)
        val = 0
        if phase < on:
            env = min(1.0, phase / edge, (on - phase) / edge)  # short fade avoids clicks
            val = int(0.5 * env * 32767 * math.sin(2 * math.pi * freq * sec))
        frames += struct.pack("<h", val)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _ringback_prompt(props):
    bucket, key = props["Bucket"], props["Key"]
    s3.put_object(Bucket=bucket, Key=key, Body=_ringback_wav(), ContentType="audio/wav")
    uri = "s3://%s/%s" % (bucket, key)
    return uri, {"S3Uri": uri}


def _empty_bucket(bucket):
    paginator = s3.get_paginator("list_object_versions")
    try:
        for page in paginator.paginate(Bucket=bucket):
            keys = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                    for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            for i in range(0, len(keys), 1000):
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys[i:i + 1000], "Quiet": True})
    except s3.exceptions.NoSuchBucket:
        pass


def _empty_buckets(props):
    return "empty-buckets", {}


OPERATIONS = {"LookupFlows": _lookup_flows, "RingbackPrompt": _ringback_prompt, "EmptyBuckets": _empty_buckets}


def handler(event, context):
    props = event["ResourceProperties"]
    if event["RequestType"] == "Delete":
        if props.get("Operation") == "EmptyBuckets":
            for bucket in props.get("Buckets", []):
                _empty_bucket(bucket)
        # Lookups own nothing; the prompt object is removed with its bucket.
        return {"PhysicalResourceId": event["PhysicalResourceId"]}
    physical_id, data = OPERATIONS[props["Operation"]](props)
    return {"PhysicalResourceId": physical_id, "Data": data}
