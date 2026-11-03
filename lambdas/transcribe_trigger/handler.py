"""Transcribes call recordings with an Amazon Transcribe batch job and writes the transcript back to S3.

Triggered by S3 ObjectCreated events under RECORDINGS_PREFIX. Output is encrypted with OUTPUT_KMS_KEY_ARN.

Language: the language selected in the IVR is used when available. The contact ID is taken from the
recording key and connect:GetContactAttributes returns the `language` attribute, which maps to:
  mandarin -> zh-CN, cantonese -> zh-HK, english -> en-US, spanish -> es-US
Only when the attribute is missing does the job fall back to IdentifyLanguage over the same languages.
Automatic identification tends to confuse Cantonese (zh-HK) with Mandarin (zh-CN), and the IVR already knows
which language the caller chose.
"""
import os
import re
import urllib.parse

import boto3

TRANSCRIBE = boto3.client("transcribe")
CONNECT = boto3.client("connect")

RECORDINGS_PREFIX = os.environ.get("RECORDINGS_PREFIX", "recordings/")
TRANSCRIPTS_PREFIX = os.environ.get("TRANSCRIPTS_PREFIX", "transcripts/")
INSTANCE_ID = os.environ.get("CONNECT_INSTANCE_ID", "")
# Encrypt output with the media bucket key; without it Transcribe uses SSE-S3, not the bucket default.
OUTPUT_KMS_KEY_ARN = os.environ.get("OUTPUT_KMS_KEY_ARN", "")

LANG_MAP = {"mandarin": "zh-CN", "cantonese": "zh-HK", "english": "en-US", "spanish": "es-US"}
LANG_OPTIONS = ["en-US", "zh-CN", "zh-HK", "es-US"]  # candidates for automatic identification
_CONTACT_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _job_name(key: str) -> str:
    base = re.sub(r"[^0-9a-zA-Z._-]", "-", key)
    return f"ivr-{base}"[:200]


def _language_code(contact_id):
    """Transcribe language code for the IVR language, or None to use automatic identification."""
    if not (INSTANCE_ID and contact_id):
        return None
    try:
        attrs = CONNECT.get_contact_attributes(
            InstanceId=INSTANCE_ID, InitialContactId=contact_id).get("Attributes", {})
        return LANG_MAP.get(attrs.get("language", "").lower())
    except Exception:
        return None


def handler(event, _context):
    started = []
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        if not key.startswith(RECORDINGS_PREFIX):
            continue
        if not key.lower().endswith((".wav", ".mp3", ".mp4", ".m4a", ".flac", ".ogg")):
            continue
        # IVR recordings (recordings//ivr/...) only capture voicemail, which VMX transcribes itself.
        if "/ivr/" in key:
            continue

        m = _CONTACT_RE.search(key)
        code = _language_code(m.group(0) if m else None)

        params = dict(
            TranscriptionJobName=_job_name(key),
            Media={"MediaFileUri": f"s3://{bucket}/{key}"},
            OutputBucketName=bucket,
            OutputKey=TRANSCRIPTS_PREFIX + key[len(RECORDINGS_PREFIX):] + ".json",
        )
        if OUTPUT_KMS_KEY_ARN:
            params["OutputEncryptionKMSKeyId"] = OUTPUT_KMS_KEY_ARN
        if code:
            params["LanguageCode"] = code                 # language selected in the IVR
        else:
            params["IdentifyLanguage"] = True             # fall back to automatic identification
            params["LanguageOptions"] = LANG_OPTIONS

        TRANSCRIBE.start_transcription_job(**params)
        started.append(params["TranscriptionJobName"])
    return {"started": started}
