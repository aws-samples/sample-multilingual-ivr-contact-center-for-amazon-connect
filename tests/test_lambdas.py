"""Lambda unit tests with a stubbed boto3; standard library only, no AWS credentials needed.

Run: `python3 tests/test_lambdas.py`
Covers: transcription language selection, fallback to automatic identification, prefix / extension / voicemail
filtering and KMS-encrypted output; returning-caller open case lookup and owner-first routing; case
assignment and Owner tag sync.
"""
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---- Fake boto3 in sys.modules, installed before the handlers are imported ----
RECORDED = {"jobs": [], "updates": [], "tags": [], "untags": []}
CONFIG = {"contact_attrs": {}, "cases": [], "case": {}, "status": "Available"}

USER_ARN = "arn:aws:connect:us-east-1:111122223333:instance/inst-123/agent/user-1"
CASE_ARN = "arn:aws:cases:us-east-1:111122223333:domain/dom-1/case/case-1"


class _FakeTranscribe:
    def start_transcription_job(self, **kw):
        RECORDED["jobs"].append(kw)


class _FakeConnect:
    def get_contact_attributes(self, **_kw):
        return {"Attributes": CONFIG["contact_attrs"]}

    def describe_user(self, **kw):
        return {"User": {"Username": "agent-" + kw["UserId"]}}

    def get_current_user_data(self, **_kw):
        if CONFIG["status"] is None:
            return {"UserDataList": []}
        return {"UserDataList": [{"Status": {"StatusName": CONFIG["status"]}}]}


class _FakeCases:
    def search_cases(self, **kw):
        RECORDED["search"] = kw
        return {"cases": CONFIG["cases"]}

    def get_case(self, **_kw):
        return CONFIG["case"]

    def update_case(self, **kw):
        RECORDED["updates"].append(kw)

    def tag_resource(self, **kw):
        RECORDED["tags"].append(kw)

    def untag_resource(self, **kw):
        RECORDED["untags"].append(kw)


def _fake_client(svc):
    return {"transcribe": _FakeTranscribe(), "connect": _FakeConnect(),
            "connectcases": _FakeCases()}[svc]


def _install_fakes():
    boto3 = types.ModuleType("boto3")
    boto3.client = _fake_client
    sys.modules["boto3"] = boto3


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_install_fakes()
os.environ.update({
    "CONNECT_INSTANCE_ID": "inst-123",
    "OUTPUT_KMS_KEY_ARN": "arn:aws:kms:us-east-1:111122223333:key/k-1",
    "CASES_DOMAIN_ID": "dom-1",
    "PROFILES_DOMAIN": "profiles-dom",
    "DEFAULT_QUEUE_ARN": "arn:aws:connect:us-east-1:111122223333:instance/inst-123/queue/q-main",
    "VOICEMAIL_TEMPLATE_ID": "tpl-voicemail",
})
transcribe = _load(ROOT / "lambdas/transcribe_trigger/handler.py", "trans_h")
lookup = _load(ROOT / "lambdas/case_owner_lookup/handler.py", "lookup_h")
assign = _load(ROOT / "lambdas/cases_auto_assign/handler.py", "assign_h")

UUID = "11111111-2222-3333-4444-555555555555"


def _s3_event(key):
    return {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": key}}}]}


def _field(fid, value):
    return {"id": fid, "value": {"stringValue": value}}


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        RECORDED["jobs"].clear()
        CONFIG["contact_attrs"] = {}

    def test_explicit_language_by_keypress(self):
        for lang, code in [("mandarin", "zh-CN"), ("cantonese", "zh-HK"),
                           ("english", "en-US"), ("spanish", "es-US")]:
            RECORDED["jobs"].clear()
            CONFIG["contact_attrs"] = {"language": lang}
            transcribe.handler(_s3_event(f"recordings/2026/10/12/{UUID}_x.wav"), None)
            self.assertEqual(len(RECORDED["jobs"]), 1)
            job = RECORDED["jobs"][0]
            self.assertEqual(job.get("LanguageCode"), code)
            self.assertNotIn("IdentifyLanguage", job)   # explicit language, no automatic identification

    def test_fallback_identify_language(self):
        CONFIG["contact_attrs"] = {}        # attribute not available
        transcribe.handler(_s3_event(f"recordings/a/{UUID}_x.wav"), None)
        job = RECORDED["jobs"][0]
        self.assertTrue(job.get("IdentifyLanguage"))
        self.assertIn("zh-HK", job["LanguageOptions"])

    def test_skip_non_recording_prefix(self):
        transcribe.handler(_s3_event(f"other/{UUID}_x.wav"), None)
        self.assertEqual(RECORDED["jobs"], [])

    def test_skip_ivr_voicemail_recording(self):
        CONFIG["contact_attrs"] = {"language": "english"}
        transcribe.handler(_s3_event(f"recordings//ivr/2026/10/12/{UUID}_x.wav"), None)
        self.assertEqual(RECORDED["jobs"], [])

    def test_skip_non_audio(self):
        transcribe.handler(_s3_event(f"recordings/a/{UUID}_x.txt"), None)
        self.assertEqual(RECORDED["jobs"], [])

    def test_output_goes_to_transcripts_prefix(self):
        CONFIG["contact_attrs"] = {"language": "english"}
        transcribe.handler(_s3_event(f"recordings/day/{UUID}_x.wav"), None)
        self.assertTrue(RECORDED["jobs"][0]["OutputKey"].startswith("transcripts/"))

    def test_output_encrypted_with_cmk(self):
        CONFIG["contact_attrs"] = {"language": "english"}
        transcribe.handler(_s3_event(f"recordings/day/{UUID}_x.wav"), None)
        self.assertEqual(RECORDED["jobs"][0]["OutputEncryptionKMSKeyId"],
                         os.environ["OUTPUT_KMS_KEY_ARN"])


class CaseOwnerLookupTests(unittest.TestCase):
    CTX = types.SimpleNamespace(
        invoked_function_arn="arn:aws:lambda:us-east-1:111122223333:function:case-owner-lookup")

    def setUp(self):
        CONFIG["cases"] = []
        CONFIG["status"] = "Available"

    def _event(self, profile_id="p-1"):
        attrs = {"vm_profile_id": profile_id} if profile_id else {}
        return {"Details": {"ContactData": {
            "InstanceARN": "arn:aws:connect:us-east-1:111122223333:instance/inst-123",
            "Attributes": attrs}}}

    def _owned_case(self):
        return [{"fields": [_field("reference_number", "12345678"),
                            {"id": "assigned_user", "value": {"userArnValue": USER_ARN}}]}]

    def test_no_profile_no_case(self):
        out = lookup.handler(self._event(profile_id=""), self.CTX)
        self.assertEqual(out["has_open_case"], "false")
        self.assertEqual(out["route_to_owner"], "false")

    def test_searches_open_cases_by_profile_arn(self):
        lookup.handler(self._event(), self.CTX)
        conds = RECORDED["search"]["filter"]["andAll"]
        values = [c["field"]["equalTo"]["value"]["stringValue"] for c in conds]
        self.assertIn("arn:aws:profile:us-east-1:111122223333:domains/profiles-dom/profiles/p-1", values)
        self.assertIn("open", values)

    def test_owner_available_routes_to_owner(self):
        CONFIG["cases"] = self._owned_case()
        out = lookup.handler(self._event(), self.CTX)
        self.assertEqual(out["has_open_case"], "true")
        self.assertEqual(out["open_case_ref"], "12345678")
        self.assertEqual(out["open_case_owner"], "agent-user-1")
        self.assertEqual(out["route_to_owner"], "true")
        step = out["RoutingCriteria"]["Steps"][0]
        self.assertEqual(step["Expression"]["AttributeCondition"]["MatchCriteria"]
                         ["AgentsCriteria"]["AgentIds"], ["user-1"])
        self.assertEqual(step["Expiry"]["DurationInSeconds"], 20)

    def test_owner_not_available_no_routing(self):
        for status in ("Offline", "Break", None):
            CONFIG["status"] = status
            CONFIG["cases"] = self._owned_case()
            out = lookup.handler(self._event(), self.CTX)
            self.assertEqual(out["has_open_case"], "true")
            self.assertEqual(out["route_to_owner"], "false")
            self.assertNotIn("RoutingCriteria", out)

    def test_unassigned_case_notice_only(self):
        CONFIG["cases"] = [{"fields": [_field("reference_number", "87654321")]}]
        out = lookup.handler(self._event(), self.CTX)
        self.assertEqual(out["has_open_case"], "true")
        self.assertEqual(out["route_to_owner"], "false")
        self.assertEqual(out["open_case_ref"], "87654321")


class CasesAutoAssignTests(unittest.TestCase):
    def setUp(self):
        for k in ("updates", "tags", "untags"):
            RECORDED[k].clear()

    def _event(self, event_type, changed=None):
        detail = {"eventType": event_type, "case": {"caseId": "case-1"}}
        if changed is not None:
            detail["changedFieldIds"] = changed
        return {"detail": detail, "resources": [CASE_ARN]}

    def _case(self, template="tpl-agent1", **fields):
        return {"templateId": template,
                "fields": [_field(k, v) for k, v in fields.items() if v]}

    def test_created_by_agent_assigns_creator_and_tags(self):
        CONFIG["case"] = self._case(created_by=USER_ARN)
        assign.handler(self._event("CASE.CREATED"), None)
        updated = {f["id"]: f["value"]["stringValue"] for f in RECORDED["updates"][0]["fields"]}
        self.assertEqual(updated["assigned_user"], USER_ARN)
        self.assertEqual(updated["assigned_queue"], os.environ["DEFAULT_QUEUE_ARN"])
        self.assertEqual(RECORDED["tags"][0]["tags"], {"Owner": "agent-user-1"})

    def test_created_by_lambda_stays_in_pool(self):
        CONFIG["case"] = self._case(template="tpl-voicemail")
        assign.handler(self._event("CASE.CREATED"), None)
        self.assertEqual(RECORDED["tags"], [])          # template already set Owner=pool; no change
        self.assertNotIn("assigned_user",
                         [f["id"] for u in RECORDED["updates"] for f in u["fields"]])

    def test_reassign_updates_owner_tag(self):
        CONFIG["case"] = self._case(assigned_user=USER_ARN)
        assign.handler(self._event("CASE.UPDATED", ["assigned_user"]), None)
        self.assertEqual(RECORDED["tags"][0]["tags"], {"Owner": "agent-user-1"})

    def test_unassigned_voicemail_returns_to_pool(self):
        CONFIG["case"] = self._case(template="tpl-voicemail")
        assign.handler(self._event("CASE.UPDATED", ["assigned_user"]), None)
        self.assertEqual(RECORDED["tags"][0]["tags"], {"Owner": "pool"})

    def test_unassigned_regular_case_untagged(self):
        CONFIG["case"] = self._case()
        assign.handler(self._event("CASE.UPDATED", ["assigned_user"]), None)
        self.assertEqual(RECORDED["untags"][0]["tagKeys"], ["Owner"])

    def test_update_without_assignee_change_is_ignored(self):
        CONFIG["case"] = self._case(assigned_user=USER_ARN)
        out = assign.handler(self._event("CASE.UPDATED", ["status"]), None)
        self.assertEqual(out["status"], "skip")
        self.assertEqual(RECORDED["tags"] + RECORDED["untags"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
