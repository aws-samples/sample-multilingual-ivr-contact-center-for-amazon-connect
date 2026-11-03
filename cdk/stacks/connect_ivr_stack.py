"""Multilingual IVR contact center on Amazon Connect: the complete CDK stack.

A single `cdk deploy` creates the whole system:
- Infrastructure: KMS customer managed key, S3 media / prompts / access log buckets, transcription Lambda,
  Kinesis contact record stream
- Amazon Connect: instance, recording storage, hours of operation, queue main, routing profiles, security
  profiles (tag-based access control), agent and supervisor users, quick connects, ringback prompt,
  contact flows (ivr-main / customer queue / agent whisper) and phone number
- Customer Profiles domain and instance integration; Cases domain, fields, layout, templates (with tag
  propagation), event stream and instance association
- Lambda: voicemail case, voicemail transcript backfill, returning-caller routing, case assignment and
  voicemail fast trigger, with their EventBridge rules
- VMX V3 voicemail (official CloudFormation template, deployed as a nested stack)

Deployment parameters are set in the context of cdk.json (see the README). Settings CloudFormation does not
support (Cases instance association, event stream, template tag propagation, quick connect queue
association, phone number flow association and so on) are applied with CDK custom resources.

Security baseline: app.py adds the cdk-nag AwsSolutions rule pack, checked at synthesis; reviewed and accepted
findings are in ConnectIvrStack._acknowledge_nag_findings.
"""
from pathlib import Path

from aws_cdk import (
    Stack, RemovalPolicy, Duration, Fn, CfnOutput, CfnTag, CustomResource, Validations, Acknowledgment,
    aws_kms as kms,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_kinesis as kinesis,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_connect as connect,
    aws_customerprofiles as profiles,
    aws_cases as cases,
    aws_cloudformation as cfn,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_secretsmanager as secrets,
    custom_resources as cr,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parents[2]
LAMBDAS = ROOT / "lambdas"
FLOWS = ROOT / "flows"
RECORDINGS_PREFIX = "recordings/"
TRANSCRIPTS_PREFIX = "transcripts/"
VMX_TEMPLATE_VERSION = "2025.09.13"
OWNER_TAG = "Owner"
POOL_OWNER = "pool"
PY = lambda_.Runtime.PYTHON_3_14

AGENT_PERMISSIONS = [
    "BasicAgentAccess", "OutboundCallAccess", "TransferDestinations.View",
    "Queues.View", "Users.View", "CustomerProfiles.View", "CustomerProfiles.Edit",
    "Cases.View", "Cases.Create", "Cases.Edit", "CaseFields.View", "CaseHistory.View",
    "CaseComments.Edit", "CaseTemplates.View",
]
POOL_PERMISSIONS = [
    "Cases.View", "Cases.Edit", "CaseFields.View", "CaseHistory.View", "CaseComments.Edit",
    "CaseTemplates.View", "Queues.View", "Users.View",
]
SUPERVISOR_PERMISSIONS = [
    "BasicAgentAccess", "TransferDestinations.View", "Queues.View", "Users.View",
    "HoursOfOperation.View", "PhoneNumbers.View", "Prompts.View", "ContactFlows.View",
    "AccessMetrics", "AccessMetrics.RealTimeMetrics.Access", "AccessMetrics.HistoricalMetrics.Access",
    "AccessMetrics.AgentActivityAudit.Access", "AccessMetrics.Dashboards.Access",
    "AccessMetrics.DashboardsWithMyData.View", "RealtimeContactLens.View",
    "ManagerListenIn", "ManagerBargeIn", "ListenCallRecordings",
    "ContactSearch.View", "ContactSearchWithCharacteristics.Access", "ContactSearchWithCharacteristics.View",
    "ContactSearchWithKeywords.Access", "ContactSearchWithKeywords.View", "ContactSearchSampleContacts.View",
    "ContactLensPostContactSummary.View",
    "MetricsReports.View", "MetricsReports.Create", "MetricsReports.Edit", "MetricsReports.Publish",
    "MetricsReports.Schedule", "MetricsReports.Share",
    "CustomerProfiles.View", "Cases.View", "Cases.Edit", "CaseFields.View", "CaseHistory.View",
    "CaseTemplates.View",
]
CASE_EVENT_FIELDS = ["title", "status", "created_by", "assigned_user", "assigned_queue"]


def _flow_content(filename, mapping):
    """Read a contact flow JSON template and turn {{X}} placeholders into Fn::Sub ${X}, resolved by CloudFormation."""
    text = (FLOWS / filename).read_text(encoding="utf-8")
    for ph in mapping:
        text = text.replace("{{%s}}" % ph, "${%s}" % ph)
    if "{{" in text:
        raise ValueError("%s has placeholders without a value" % filename)
    return Fn.sub(text, mapping) if mapping else text


def _last(arn):
    """Last segment (resource ID) of an Amazon Connect resource ARN."""
    return Fn.select(3, Fn.split("/", arn))


class ConnectIvrStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, config: dict, **kw):
        super().__init__(scope, cid, **kw)
        alias = config["instance_alias"]
        agents = config["agents"]
        supervisor = config["supervisor"]
        labels = config["cases_labels"]

        # ================= Infrastructure =================
        key = kms.Key(self, "MediaKey", enable_key_rotation=True, removal_policy=RemovalPolicy.DESTROY)
        # Server access logs cannot be delivered to an SSE-KMS bucket, so the access log bucket uses SSE-S3.
        access_logs = s3.Bucket(
            self, "AccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(id="expire-1-year", expiration=Duration.days(365))],
        )
        bucket = s3.Bucket(
            self, "MediaBucket",
            encryption=s3.BucketEncryption.KMS, encryption_key=key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            server_access_logs_bucket=access_logs, server_access_logs_prefix="media-bucket/",
            # The voicemail fast trigger subscribes to this bucket's EventBridge events; keep it enabled.
            event_bridge_enabled=True,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(
                id="retain-7-years", expiration=Duration.days(2555),
                transitions=[s3.Transition(storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                                           transition_after=Duration.days(30))])],
        )
        # Amazon Connect reads prompt files from this bucket, so it uses SSE-S3.
        prompts = s3.Bucket(
            self, "PromptsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            server_access_logs_bucket=access_logs, server_access_logs_prefix="prompts-bucket/",
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
        )
        ctr_stream = kinesis.Stream(self, "CtrStream", shard_count=1, retention_period=Duration.hours(24),
                                    removal_policy=RemovalPolicy.DESTROY)

        # ================= Amazon Connect instance =================
        inst = connect.CfnInstance(
            self, "Instance", identity_management_type="CONNECT_MANAGED", instance_alias=alias,
            attributes=connect.CfnInstance.AttributesProperty(
                inbound_calls=True, outbound_calls=True, contact_lens=True, contactflow_logs=True))
        inst_arn, inst_id = inst.attr_arn, inst.attr_id
        inst_children = Fn.sub("${a}/*", {"a": inst_arn})
        prompts_policy = prompts.add_to_resource_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectAcl"], resources=[prompts.arn_for_objects("*")],
            principals=[iam.ServicePrincipal("connect.amazonaws.com")],
            conditions={"StringEquals": {"aws:SourceAccount": self.account},
                        "ArnEquals": {"aws:SourceArn": inst_arn}}))

        connect.CfnInstanceStorageConfig(
            self, "RecordingStorage", instance_arn=inst_arn, resource_type="CALL_RECORDINGS",
            storage_type="S3",
            s3_config=connect.CfnInstanceStorageConfig.S3ConfigProperty(
                bucket_name=bucket.bucket_name, bucket_prefix=RECORDINGS_PREFIX,
                encryption_config=connect.CfnInstanceStorageConfig.EncryptionConfigProperty(
                    encryption_type="KMS", key_id=key.key_arn)))
        connect.CfnInstanceStorageConfig(
            self, "CtrStreaming", instance_arn=inst_arn, resource_type="CONTACT_TRACE_RECORDS",
            storage_type="KINESIS_STREAM",
            kinesis_stream_config=connect.CfnInstanceStorageConfig.KinesisStreamConfigProperty(
                stream_arn=ctr_stream.stream_arn))

        # Transcription Lambda (S3 recording events; transcribes in the IVR language; output encrypted with the CMK)
        trans_fn = lambda_.Function(
            self, "TranscribeTriggerFn", runtime=PY, handler="handler.handler",
            code=lambda_.Code.from_asset(str(LAMBDAS / "transcribe_trigger")), timeout=Duration.seconds(30),
            log_group=self._log_group("TranscribeTriggerFn"),
            environment={"RECORDINGS_PREFIX": RECORDINGS_PREFIX, "TRANSCRIPTS_PREFIX": TRANSCRIPTS_PREFIX,
                         "OUTPUT_KMS_KEY_ARN": key.key_arn, "CONNECT_INSTANCE_ID": inst_id})
        trans_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"], resources=[bucket.arn_for_objects(RECORDINGS_PREFIX + "*")]))
        trans_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject"], resources=[bucket.arn_for_objects(TRANSCRIPTS_PREFIX + "*")]))
        trans_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:GenerateDataKey"], resources=[key.key_arn]))
        trans_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["transcribe:StartTranscriptionJob"], resources=["*"]))
        trans_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["connect:GetContactAttributes"], resources=[Fn.sub("${a}/contact/*", {"a": inst_arn})]))
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(trans_fn),
            s3.NotificationKeyFilter(prefix=RECORDINGS_PREFIX))

        # ================= Queue, routing profiles, security profiles, users =================
        hours = connect.CfnHoursOfOperation(
            self, "Hours", instance_arn=inst_arn, name="always-open", time_zone="UTC",
            config=[connect.CfnHoursOfOperation.HoursOfOperationConfigProperty(
                day=d,
                start_time=connect.CfnHoursOfOperation.HoursOfOperationTimeSliceProperty(hours=0, minutes=0),
                end_time=connect.CfnHoursOfOperation.HoursOfOperationTimeSliceProperty(hours=23, minutes=59))
                for d in ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]])
        queue = connect.CfnQueue(
            self, "MainQueue", instance_arn=inst_arn, name="main",
            description="Single queue for all languages; language is a contact attribute",
            hours_of_operation_arn=hours.attr_hours_of_operation_arn)
        q_arn = queue.attr_queue_arn
        q_id = _last(q_arn)

        media = [connect.CfnRoutingProfile.MediaConcurrencyProperty(channel=ch, concurrency=1)
                 for ch in ("VOICE", "TASK")]
        agent_rp = connect.CfnRoutingProfile(
            self, "AgentRoutingProfile", instance_arn=inst_arn, name="agent-rp",
            description="Agents: main queue, VOICE and TASK", default_outbound_queue_arn=q_arn,
            media_concurrencies=media,
            queue_configs=[connect.CfnRoutingProfile.RoutingProfileQueueConfigProperty(
                delay=0, priority=1, queue_reference=connect.CfnRoutingProfile.RoutingProfileQueueReferenceProperty(
                    channel=ch, queue_arn=q_arn)) for ch in ("VOICE", "TASK")])
        # The supervisor receives no calls: the routing profile has no queues.
        # The supervisor's case Assigned User picker lists the user quick connects on the queues of the
        # supervisor's routing profile. No flow routes contacts to this queue, so the supervisor receives no calls.
        assign_queue = connect.CfnQueue(
            self, "CaseAssignmentQueue", instance_arn=inst_arn, name="case-assignment",
            description="Lists agents for case assignment by the supervisor; receives no contacts",
            hours_of_operation_arn=hours.attr_hours_of_operation_arn)
        assign_q_id = _last(assign_queue.attr_queue_arn)
        sup_rp = connect.CfnRoutingProfile(
            self, "SupervisorRoutingProfile", instance_arn=inst_arn, name="supervisor-rp",
            description="Supervisor: case assignment queue only, receives no contacts",
            default_outbound_queue_arn=q_arn, media_concurrencies=media,
            queue_configs=[connect.CfnRoutingProfile.RoutingProfileQueueConfigProperty(
                delay=0, priority=1, queue_reference=connect.CfnRoutingProfile.RoutingProfileQueueReferenceProperty(
                    channel="VOICE", queue_arn=assign_queue.attr_queue_arn))])

        def tag_profile(lid, name, owner, perms):
            return connect.CfnSecurityProfile(
                self, lid, instance_arn=inst_arn, security_profile_name=name, permissions=perms,
                tag_restricted_resources=["Case"],
                allowed_access_control_tags=[CfnTag(key=OWNER_TAG, value=owner)])

        pool_sp = tag_profile("PoolSecurityProfile", "Agent-voicemail-pool", POOL_OWNER, POOL_PERMISSIONS)
        sup_sp = connect.CfnSecurityProfile(
            self, "SupervisorSecurityProfile", instance_arn=inst_arn, security_profile_name="Supervisor",
            permissions=SUPERVISOR_PERMISSIONS)

        def user(lid, spec, rp, sps):
            pw = secrets.Secret(
                self, lid + "Password", description="Amazon Connect password for " + spec["username"],
                generate_secret_string=secrets.SecretStringGenerator(
                    password_length=16, require_each_included_type=True, exclude_punctuation=True))
            u = connect.CfnUser(
                self, lid, instance_arn=inst_arn, username=spec["username"],
                password=pw.secret_value.unsafe_unwrap(), routing_profile_arn=rp.attr_routing_profile_arn,
                security_profile_arns=[sp.attr_security_profile_arn for sp in sps],
                identity_info=connect.CfnUser.UserIdentityInfoProperty(
                    first_name=spec["first_name"], last_name=spec["last_name"]),
                phone_config=connect.CfnUser.UserPhoneConfigProperty(
                    phone_type="SOFT_PHONE", auto_accept=False, after_contact_work_time_limit=0))
            CfnOutput(self, "Out%sPassword" % lid, value=pw.secret_arn,
                      description="Secrets Manager ARN of the password for " + spec["username"])
            return u, pw

        agent_users, user_secrets = [], []
        for i, a in enumerate(agents, start=1):
            sp = tag_profile("Agent%dSecurityProfile" % i, "Agent-" + a["username"], a["username"],
                             AGENT_PERMISSIONS)
            u, pw = user("Agent%d" % i, a, agent_rp, [sp, pool_sp])
            agent_users.append(u)
            user_secrets.append(pw)
        sup_user, sup_secret = user("Supervisor", supervisor, sup_rp, [sup_sp])
        user_secrets.append(sup_secret)

        # ================= Customer Profiles =================
        # The "amazon-connect-" prefix is required: the Amazon Connect service-linked role policy grants
        # profile:* only on domains with this prefix, which the Agent Workspace needs to read and edit profiles.
        profiles_domain = profiles.CfnDomain(
            self, "ProfilesDomain", domain_name="amazon-connect-" + alias, default_expiration_days=366,
            default_encryption_key=key.key_arn)
        ctr_type = profiles.CfnObjectType(
            self, "ProfilesCtrObjectType", domain_name=profiles_domain.domain_name, object_type_name="CTR",
            description="Amazon Connect contact records", template_id="CTR-NoInferred")
        ctr_type.node.add_dependency(profiles_domain)
        profiles_int = profiles.CfnIntegration(
            self, "ProfilesConnectIntegration", domain_name=profiles_domain.domain_name, uri=inst_arn,
            object_type_name="CTR")
        profiles_int.node.add_dependency(ctr_type)
        profile_domain_arn = Fn.sub("arn:aws:profile:${AWS::Region}:${AWS::AccountId}:domains/${d}",
                                    {"d": profiles_domain.domain_name})

        # ================= Cases =================
        cases_domain = cases.CfnDomain(self, "CasesDomain", name=alias + "-cases")
        dom_id, dom_arn = cases_domain.attr_domain_id, cases_domain.attr_domain_arn
        cases_res = [dom_arn, Fn.sub("${d}/*", {"d": dom_arn})]
        f_ticket = cases.CfnField(self, "FieldTicketNo", domain_id=dom_id, name=labels["ticket_field"], type="Text",
                                  description="6-digit voicemail ticket number")
        f_vt = cases.CfnField(self, "FieldVoicemailTranscript", domain_id=dom_id, name=labels["transcript_field"], type="Text",
                              description="Transcript of the caller's voicemail",
                              attributes=cases.CfnField.FieldAttributesProperty(
                                  text=cases.CfnField.TextAttributesProperty(is_multiline=True)))
        ff = lambda fid: cases.CfnLayout.FieldItemProperty(id=fid)
        layout = cases.CfnLayout(
            self, "CaseLayout", domain_id=dom_id, name=labels["layout"],
            content=cases.CfnLayout.LayoutContentProperty(basic=cases.CfnLayout.BasicLayoutProperty(
                top_panel=cases.CfnLayout.LayoutSectionsProperty(sections=[cases.CfnLayout.SectionProperty(
                    field_group=cases.CfnLayout.FieldGroupProperty(name=labels["summary_section"], fields=[ff("summary")]))]),
                more_info=cases.CfnLayout.LayoutSectionsProperty(sections=[cases.CfnLayout.SectionProperty(
                    field_group=cases.CfnLayout.FieldGroupProperty(name=labels["details_section"], fields=[
                        ff("assigned_queue"), ff("assigned_user"), ff("created_datetime"),
                        ff("last_updated_datetime"), ff("reference_number"), ff("customer_id"),
                        ff(f_ticket.attr_field_id), ff(f_vt.attr_field_id)]))]))))

        def template(lid, name, owner=None):
            t = cases.CfnTemplate(
                self, lid, domain_id=dom_id, name=name, status="Active",
                layout_configuration=cases.CfnTemplate.LayoutConfigurationProperty(
                    default_layout=layout.attr_layout_id),
                required_fields=[cases.CfnTemplate.RequiredFieldProperty(field_id="title")])
            if owner:
                # Tag propagation: tag-restricted agents can only create cases that carry an Owner tag (no CloudFormation property yet).
                call = self._sdk("ConnectCases", "updateTemplate", lid + "TagPropagation",
                                 domainId=dom_id, templateId=t.attr_template_id,
                                 tagPropagationConfigurations=[{"resourceType": "Cases",
                                                                "tagMap": {OWNER_TAG: owner}}])
                cr.AwsCustomResource(
                    self, lid + "TagPropagation", install_latest_aws_sdk=True, on_create=call, on_update=call,
                    policy=cr.AwsCustomResourcePolicy.from_statements([iam.PolicyStatement(
                        actions=["cases:UpdateTemplate"], resources=cases_res)]))
            return t

        template("TemplateStandard", labels["standard_template"])
        vm_template = template("TemplateVoicemail", labels["voicemail_template"], POOL_OWNER)
        for i, a in enumerate(agents, start=1):
            template("TemplateAgent%d" % i, labels["agent_template_prefix"] + a["username"], a["username"])

        cr.AwsCustomResource(
            self, "CasesInstanceAssociation",
            on_create=cr.AwsSdkCall(
                service="Connect", action="createIntegrationAssociation",
                parameters={"InstanceId": inst_id, "IntegrationType": "CASES_DOMAIN", "IntegrationArn": dom_arn},
                physical_resource_id=cr.PhysicalResourceId.from_response("IntegrationAssociationId")),
            on_delete=cr.AwsSdkCall(
                service="Connect", action="deleteIntegrationAssociation",
                parameters={"InstanceId": inst_id, "IntegrationAssociationId": cr.PhysicalResourceIdReference()}),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(actions=["connect:CreateIntegrationAssociation",
                                             "connect:DeleteIntegrationAssociation"],
                                    resources=[inst_arn, inst_children]),
                iam.PolicyStatement(actions=["cases:GetDomain"], resources=cases_res),
                # Associating the Cases domain makes Amazon Connect set up event integration and its service-linked role as the caller.
                iam.PolicyStatement(actions=["events:PutRule", "events:PutTargets", "events:DeleteRule",
                                             "events:RemoveTargets", "iam:CreateServiceLinkedRole"], resources=["*"]),
                iam.PolicyStatement(actions=["iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
                                             "iam:GetRole"],
                                    resources=[Fn.sub("arn:aws:iam::${AWS::AccountId}:role/aws-service-role/"
                                                      "connect.amazonaws.com/*")])]))
        cases_events = cr.AwsCustomResource(
            self, "CasesEventStream",
            on_create=self._sdk("ConnectCases", "putCaseEventConfiguration", "CasesEventStream",
                                domainId=dom_id, eventBridge={"enabled": True, "includedData": {
                                    "caseData": {"fields": [{"id": f} for f in CASE_EVENT_FIELDS]},
                                    "relatedItemData": {"includeContent": False}}}),
            policy=cr.AwsCustomResourcePolicy.from_statements([iam.PolicyStatement(
                actions=["cases:PutCaseEventConfiguration"], resources=cases_res)]))
        cases_events.node.add_dependency(cases_domain)

        # ================= Application Lambda functions =================
        def fn(lid, folder, env, timeout=15):
            return lambda_.Function(self, lid, runtime=PY, handler="handler.handler",
                                    code=lambda_.Code.from_asset(str(LAMBDAS / folder)),
                                    timeout=Duration.seconds(timeout), environment=env,
                                    log_group=self._log_group(lid))

        vm_case_fn = fn("VoicemailCaseFn", "vmx_voicemail_case", {
            "CASES_DOMAIN_ID": dom_id, "CASES_TEMPLATE_ID": vm_template.attr_template_id,
            "PROFILES_DOMAIN": profiles_domain.domain_name, "F_TICKET": f_ticket.attr_field_id})
        vm_case_fn.add_to_role_policy(iam.PolicyStatement(
            # TagResource: the voicemail template propagates Owner=pool to the case at creation.
            actions=["cases:CreateCase", "cases:SearchCases", "cases:GetCase", "cases:CreateRelatedItem",
                     "cases:TagResource"],
            resources=cases_res))
        vm_case_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["profile:SearchProfiles", "profile:GetProfile", "profile:GetProfileObjectType"],
            resources=[profile_domain_arn, Fn.sub("${d}/*", {"d": profile_domain_arn})]))
        vm_case_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"], resources=[key.key_arn]))

        backfill_fn = fn("TranscriptBackfillFn", "vmx_voicemail_transcript_backfill", {
            "CASES_DOMAIN_ID": dom_id, "F_VT": f_vt.attr_field_id, "INSTANCE_ID": inst_id}, timeout=30)
        backfill_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cases:UpdateCase", "cases:GetCase"], resources=cases_res))
        backfill_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["connect:GetContactAttributes"], resources=[Fn.sub("${a}/contact/*", {"a": inst_arn})]))
        # Voicemail transcripts come from VMX (bucket vmx3-transcripts-<alias>); voicemail is not transcribed again.
        vmx_transcripts_bucket = "vmx3-transcripts-" + alias
        backfill_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"], resources=["arn:aws:s3:::%s/*" % vmx_transcripts_bucket]))

        lookup_fn = fn("CaseOwnerLookupFn", "case_owner_lookup", {
            "CASES_DOMAIN_ID": dom_id, "PROFILES_DOMAIN": profiles_domain.domain_name,
            "OWNER_WAIT_SECONDS": str(config.get("owner_wait_seconds", 20))}, timeout=6)
        lookup_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cases:SearchCases", "cases:GetCase"], resources=cases_res))
        lookup_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["connect:DescribeUser", "connect:GetCurrentUserData"], resources=[inst_arn, inst_children]))

        assign_fn = fn("CasesAutoAssignFn", "cases_auto_assign", {
            "CASES_DOMAIN_ID": dom_id, "DEFAULT_QUEUE_ARN": q_arn,
            "VOICEMAIL_TEMPLATE_ID": vm_template.attr_template_id})
        assign_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cases:GetCase", "cases:UpdateCase", "cases:TagResource", "cases:UntagResource",
                     "cases:ListTagsForResource"], resources=cases_res))
        assign_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["connect:DescribeUser", "connect:DescribeQueue"],
            resources=[Fn.sub("${a}/agent/*", {"a": inst_arn}), Fn.sub("${a}/queue/*", {"a": inst_arn})]))

        recording_processor = "VMX3-RecordingProcessor-" + alias
        rp_arn = Fn.sub("arn:aws:lambda:${AWS::Region}:${AWS::AccountId}:function:" + recording_processor)
        fast_fn = fn("VoicemailFastTriggerFn", "vmx_fasttrigger",
                     {"instance_id": inst_id, "recording_processor": rp_arn}, timeout=30)
        fast_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["connect:GetContactAttributes"], resources=[Fn.sub("${a}/contact/*", {"a": inst_arn})]))
        fast_fn.add_to_role_policy(iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[rp_arn]))

        # Functions invoked from contact flows must be associated with the instance and invocable by Amazon Connect.
        associations = {}
        for lid, f in (("VoicemailCaseFnAssociation", vm_case_fn), ("CaseOwnerLookupFnAssociation", lookup_fn)):
            f.add_permission(lid + "Invoke", principal=iam.ServicePrincipal("connect.amazonaws.com"),
                             source_account=self.account, source_arn=inst_arn)
            associations[lid] = connect.CfnIntegrationAssociation(
                self, lid, instance_id=inst_arn, integration_type="LAMBDA_FUNCTION", integration_arn=f.function_arn)

        # EventBridge: transcript backfill (VMX transcripts bucket), voicemail fast trigger (media bucket), case assignment (Cases events)
        def s3_rule(lid, bucket_name, key_filter, target):
            events.Rule(self, lid, event_pattern=events.EventPattern(
                source=["aws.s3"], detail_type=["Object Created"],
                detail={"bucket": {"name": [bucket_name]}, "object": {"key": [key_filter]}}),
                targets=[targets.LambdaFunction(target, retry_attempts=2)])
        # The VMX transcripts bucket has EventBridge notifications enabled; keys are YYYY/MM/DD/<contactId>.json.
        s3_rule("TranscriptBackfillRule", vmx_transcripts_bucket, {"suffix": ".json"}, backfill_fn)
        # Amazon Connect appends "/" to the storage prefix for IVR recordings, so keys are recordings//ivr/...
        s3_rule("VoicemailFastTriggerRule", bucket.bucket_name, {"prefix": RECORDINGS_PREFIX + "/ivr/"}, fast_fn)
        events.Rule(self, "CasesAutoAssignRule", event_pattern=events.EventPattern(
            source=["aws.cases"], detail_type=["Amazon Connect Cases Change"],
            detail={"eventType": ["CASE.CREATED", "CASE.UPDATED"]}, resources=[dom_arn]),
            targets=[targets.LambdaFunction(assign_fn, retry_attempts=2)])

        # ================= Deployment helper custom resources (flow lookup by name, ringback prompt) =================
        helper_fn = fn("ConnectHelperFn", "cfn_connect_helper", {}, timeout=120)
        helper_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["connect:ListContactFlows", "connect:ListContactFlowModules"],
            resources=[inst_arn, inst_children]))
        prompts.grant_put(helper_fn)
        provider = cr.Provider(self, "ConnectHelperProvider", on_event_handler=helper_fn,
                               log_group=logs.LogGroup(self, "ConnectHelperProviderLogs",
                                                       retention=logs.RetentionDays.ONE_MONTH,
                                                       removal_policy=RemovalPolicy.DESTROY))

        ringback_audio = self._helper(provider, "RingbackAudio", Operation="RingbackPrompt",
                                      Bucket=prompts.bucket_name, Key="prompts/ringback_cn.wav")
        ringback = connect.CfnPrompt(self, "RingbackPrompt", instance_arn=inst_arn, name="Ringback_CN_450",
                                     description="China-standard ringback tone, 450 Hz, 30 s",
                                     s3_uri=ringback_audio.get_att_string("S3Uri"))
        if prompts_policy.policy_dependable:
            ringback.node.add_dependency(prompts_policy.policy_dependable)

        defaults = self._helper(provider, "DefaultFlowLookup", Operation="LookupFlows", InstanceId=inst_id,
                                Flows={"AgentTransfer": "Default agent transfer"})

        # Quick connects (the agents listed in a case's Assigned User picker), associated with queue main
        qc_ids = []
        for i, (a, u) in enumerate(zip(agents, agent_users), start=1):
            qc = connect.CfnQuickConnect(
                self, "QuickConnectAgent%d" % i, instance_arn=inst_arn, name=a["username"],
                quick_connect_config=connect.CfnQuickConnect.QuickConnectConfigProperty(
                    quick_connect_type="USER", user_config=connect.CfnQuickConnect.UserQuickConnectConfigProperty(
                        user_arn=u.attr_user_arn, contact_flow_arn=Fn.sub(
                            "${a}/contact-flow/${f}", {"a": inst_arn, "f": defaults.get_att_string("AgentTransfer")}))))
            qc_ids.append(_last(qc.attr_quick_connect_arn))
        qc_params = {"InstanceId": inst_id, "QueueId": q_id, "QuickConnectIds": qc_ids}
        cr.AwsCustomResource(
            self, "QueueQuickConnects",
            on_create=cr.AwsSdkCall(service="Connect", action="associateQueueQuickConnects", parameters=qc_params,
                                    physical_resource_id=cr.PhysicalResourceId.of("queue-quick-connects")),
            on_delete=cr.AwsSdkCall(service="Connect", action="disassociateQueueQuickConnects", parameters=qc_params),
            policy=cr.AwsCustomResourcePolicy.from_statements([iam.PolicyStatement(
                actions=["connect:AssociateQueueQuickConnects", "connect:DisassociateQueueQuickConnects"],
                resources=[inst_arn, inst_children])]))
        assign_qc = {"InstanceId": inst_id, "QueueId": assign_q_id, "QuickConnectIds": qc_ids}
        assign_qc_call = cr.AwsSdkCall(service="Connect", action="associateQueueQuickConnects", parameters=assign_qc,
                                       physical_resource_id=cr.PhysicalResourceId.of("assignment-queue-quick-connects"))
        cr.AwsCustomResource(
            self, "AssignmentQueueQuickConnects", on_create=assign_qc_call, on_update=assign_qc_call,
            on_delete=cr.AwsSdkCall(service="Connect", action="disassociateQueueQuickConnects", parameters=assign_qc),
            policy=cr.AwsCustomResourcePolicy.from_statements([iam.PolicyStatement(
                actions=["connect:AssociateQueueQuickConnects", "connect:DisassociateQueueQuickConnects"],
                resources=[inst_arn, inst_children])]))

        # ================= VMX V3 voicemail (official template, nested stack) =================
        vmx = cfn.CfnStack(
            self, "VoicemailExpress",
            template_url="https://vmx-source-%s.s3.%s.amazonaws.com/vmx3/%s/cloudformation/vmx3.yaml"
                         % (self.region, self.region, VMX_TEMPLATE_VERSION),
            parameters={
                "AWSRegion": self.region, "ConnectInstanceAlias": alias, "ConnectInstanceARN": inst_arn,
                "ConnectCTRStreamARN": ctr_stream.stream_arn, "ConnectRecordingsBucketArn": bucket.bucket_arn,
                "VMTestAgentId": agents[0]["username"], "VMTestQueueARN": q_arn,
                "VMDefaultMode": "AmazonConnectGuidedTask", "EnableVMToConnectGuidedTask": "yes",
                "EnableVMToConnectTask": "yes",
                "EXPTemplateVersion": VMX_TEMPLATE_VERSION})
        # A new instance needs time to propagate its defaults; deploy VMX after the queue, users and prompt so its flows can be created.
        for dep in [queue, ringback, *agent_users, sup_user, profiles_int]:
            vmx.node.add_dependency(dep)

        # VMX processing roles read KMS-encrypted recordings
        for role in ("VMX3_Recording_Processor_Role_", "VMX3_Transcriber_Role_", "VMX3_Packager_Role_",
                     "VMX3_Guided_Flow_Role_"):
            lid = role.replace("_", "")
            pol = iam.Policy(self, "VmxKms" + lid, statements=[iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"], resources=[key.key_arn])])
            pol.attach_to_role(iam.Role.from_role_name(self, "Vmx" + lid, role + alias))
            pol.node.add_dependency(vmx)

        # Voicemail is processed from S3 events: disable the VMX Kinesis event source mapping to avoid double processing
        esm = cr.AwsCustomResource(
            self, "VmxKinesisMappingLookup",
            on_create=cr.AwsSdkCall(service="Lambda", action="listEventSourceMappings",
                                    parameters={"FunctionName": recording_processor},
                                    physical_resource_id=cr.PhysicalResourceId.of("vmx-esm-lookup"),
                                    output_paths=["EventSourceMappings.0.UUID"]),
            policy=cr.AwsCustomResourcePolicy.from_statements([iam.PolicyStatement(
                actions=["lambda:ListEventSourceMappings"], resources=["*"])]))
        esm.node.add_dependency(vmx)
        cr.AwsCustomResource(
            self, "VmxKinesisMappingDisable",
            on_create=cr.AwsSdkCall(service="Lambda", action="updateEventSourceMapping",
                                    parameters={"UUID": esm.get_response_field("EventSourceMappings.0.UUID"),
                                                "Enabled": False},
                                    physical_resource_id=cr.PhysicalResourceId.of("vmx-esm-disabled")),
            policy=cr.AwsCustomResourcePolicy.from_statements([iam.PolicyStatement(
                actions=["lambda:UpdateEventSourceMapping"],
                resources=[Fn.sub("arn:aws:lambda:${AWS::Region}:${AWS::AccountId}:event-source-mapping:*")])]))

        vmx_ids = self._helper(provider, "VmxFlowLookup", Operation="LookupFlows", InstanceId=inst_id,
                               Flows={"AgentWhisper": "VMX3_In_Queue_Agent_Whisper_" + alias,
                                      "CustomerWhisper": "VMX3_In_Queue_Customer_Whisper_" + alias,
                                      "TaskFlow": "VMX3_Basic_Task_Flow_" + alias,
                                      "GuidedTaskFlow": "VMX3_Guided_Task_Flow_" + alias},
                               Modules={"CoreModule": "VMX3_Main_VM_Module_" + alias})
        vmx_ids.node.add_dependency(vmx)

        # The VMX template does not empty its buckets on deletion; empty them first so cdk destroy succeeds.
        vmx_buckets = ["vmx3-recordings-" + alias, "vmx3-transcripts-" + alias]
        helper_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:ListBucket", "s3:ListBucketVersions"],
            resources=["arn:aws:s3:::%s" % b for b in vmx_buckets]))
        helper_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:DeleteObject", "s3:DeleteObjectVersion"],
            resources=["arn:aws:s3:::%s/*" % b for b in vmx_buckets]))
        vmx_cleanup = self._helper(provider, "VmxBucketCleanup", Operation="EmptyBuckets", Buckets=vmx_buckets)
        vmx_cleanup.node.add_dependency(vmx)

        # ================= Contact flows =================
        base = {"REGION": self.region, "ACCOUNT_ID": self.account, "INSTANCE_ID": inst_id}
        whisper = connect.CfnContactFlow(
            self, "CaseNoticeWhisperFlow", instance_arn=inst_arn, name="agent-whisper-case-notice",
            type="AGENT_WHISPER", description="Announces the caller's open case to the agent",
            content=_flow_content("agent-whisper-case-notice.flow.json", {}))
        customer_queue = connect.CfnContactFlow(
            self, "CustomerQueueFlow", instance_arn=inst_arn, name="ivr-customer-queue",
            type="CUSTOMER_QUEUE", description="Ringback, then voicemail offer (VMX V3)",
            content=_flow_content("customer-queue.flow.json", dict(base, **{
                "INSTANCE_ALIAS": alias, "VOICEMAIL_CASE_FUNCTION_ARN": vm_case_fn.function_arn,
                "RINGBACK_PROMPT_ID": _last(ringback.attr_prompt_arn),
                "VMX_AGENT_WHISPER_FLOW_ID": vmx_ids.get_att_string("AgentWhisper"),
                "VMX_CUSTOMER_WHISPER_FLOW_ID": vmx_ids.get_att_string("CustomerWhisper")})))
        customer_queue.node.add_dependency(associations["VoicemailCaseFnAssociation"])
        ivr_main = connect.CfnContactFlow(
            self, "IvrMainFlow", instance_arn=inst_arn, name="ivr-main", type="CONTACT_FLOW",
            description="Inbound: customer identification, language menu, returning-caller routing, queue",
            content=_flow_content("ivr-main.flow.json", dict(base, **{
                "QUEUE_MAIN_ID": q_id,
                "CUSTOMER_QUEUE_FLOW_ID": _last(customer_queue.attr_contact_flow_arn),
                "CASE_NOTICE_WHISPER_FLOW_ID": _last(whisper.attr_contact_flow_arn),
                "CASE_OWNER_LOOKUP_FUNCTION_ARN": lookup_fn.function_arn,
                "VMX_AGENT_WHISPER_FLOW_ID": vmx_ids.get_att_string("AgentWhisper"),
                "VMX_CUSTOMER_WHISPER_FLOW_ID": vmx_ids.get_att_string("CustomerWhisper"),
                "VMX_TASK_FLOW_ID": vmx_ids.get_att_string("TaskFlow"),
                "VMX_GUIDED_TASK_FLOW_ID": vmx_ids.get_att_string("GuidedTaskFlow"),
                "VMX_CORE_MODULE_ID": vmx_ids.get_att_string("CoreModule")})))
        ivr_main.node.add_dependency(associations["CaseOwnerLookupFnAssociation"])

        # ================= Phone number =================
        if config.get("claim_phone_number", True):
            number = connect.CfnPhoneNumber(
                self, "PhoneNumber", target_arn=inst_arn, country_code=config.get("phone_country_code", "US"),
                type="DID", description="Inbound number for ivr-main")
            number_id = Fn.select(1, Fn.split("phone-number/", number.attr_phone_number_arn))
            cr.AwsCustomResource(
                self, "PhoneNumberFlow",
                on_create=cr.AwsSdkCall(service="Connect", action="associatePhoneNumberContactFlow",
                                        parameters={"PhoneNumberId": number_id, "InstanceId": inst_id,
                                                    "ContactFlowId": _last(ivr_main.attr_contact_flow_arn)},
                                        physical_resource_id=cr.PhysicalResourceId.of("phone-number-flow")),
                on_delete=cr.AwsSdkCall(service="Connect", action="disassociatePhoneNumberContactFlow",
                                        parameters={"PhoneNumberId": number_id, "InstanceId": inst_id}),
                policy=cr.AwsCustomResourcePolicy.from_statements([iam.PolicyStatement(
                    actions=["connect:AssociatePhoneNumberContactFlow", "connect:DisassociatePhoneNumberContactFlow"],
                    resources=[number.attr_phone_number_arn, inst_arn, inst_children])]))
            CfnOutput(self, "OutPhoneNumber", value=number.attr_address, description="Inbound phone number")

        CfnOutput(self, "OutInstanceId", value=inst_id)
        CfnOutput(self, "OutLoginUrl", value="https://%s.my.connect.aws/" % alias,
                  description="Amazon Connect admin website and Agent Workspace")
        CfnOutput(self, "OutMediaBucket", value=bucket.bucket_name)
        CfnOutput(self, "OutCasesDomainId", value=dom_id)

        self._acknowledge_nag_findings(access_logs, user_secrets, ctr_stream)

    # ---- helpers ----
    @staticmethod
    def _sdk(service, action, pid, **params):
        return cr.AwsSdkCall(service=service, action=action, parameters=params,
                             physical_resource_id=cr.PhysicalResourceId.of(pid))

    def _log_group(self, lid):
        """Lambda log group, retained for 1 month and deleted with the stack."""
        return logs.LogGroup(self, lid + "Logs", retention=logs.RetentionDays.ONE_MONTH,
                             removal_policy=RemovalPolicy.DESTROY)

    def _helper(self, provider, lid, **props):
        return CustomResource(self, lid, service_token=provider.service_token, properties=props)

    def _acknowledge_nag_findings(self, access_logs, user_secrets, ctr_stream):
        """cdk-nag (AwsSolutions) findings reviewed and accepted, each with its reason for audit."""
        def ack(scope, rule, reason):
            Validations.of(scope).acknowledge(Acknowledgment(id="AwsSolutions::" + rule, reason=reason))

        ack(access_logs, "AwsSolutions-S1",
            "This bucket is the server access log destination for the other buckets; logging it into itself would loop.")
        for s in user_secrets:
            ack(s, "AwsSolutions-SMG4",
                "Amazon Connect user passwords cannot be rotated by Secrets Manager without a custom rotation function; "
                "rotate them from the Connect admin website.")
        ack(ctr_stream, "AwsSolutions-KDS3",
            "The contact record stream uses the AWS managed KMS key; a customer managed key would also require granting "
            "Amazon Connect and the VMX functions access to it.")
        ack(self, "AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]",
            "AWSLambdaBasicExecutionRole only grants CloudWatch Logs writes for each function's own log group.")
        inst = '{"a":{"Fn::GetAtt":["Instance","Arn"]}}'
        wildcard_findings = {
            "Resource::<MediaBucketBCBB02BA.Arn>/recordings/*":
                "Transcribe reads call recordings with the caller's identity; scoped to the recordings/ prefix.",
            "Resource::<MediaBucketBCBB02BA.Arn>/transcripts/*":
                "Call transcripts are written under the transcripts/ prefix only.",
            "Resource::arn:aws:s3:::vmx3-transcripts-%s/*" % self.node.try_get_context("instance_alias"):
                "Reads voicemail transcripts written by VMX; object keys are known only when the event arrives. The "
                "deployment helper also empties this bucket on stack deletion.",
            "Resource::arn:aws:s3:::vmx3-recordings-%s/*" % self.node.try_get_context("instance_alias"):
                "The deployment helper empties the VMX recordings bucket on stack deletion so CloudFormation can "
                "delete it; scoped to this stack's VMX bucket.",
            "Resource::*":
                "Actions without resource-level permissions: transcribe:StartTranscriptionJob, "
                "lambda:ListEventSourceMappings, logs:PutRetentionPolicy, and the EventBridge / service-linked role "
                "setup Amazon Connect performs when the Cases domain is associated.",
            'Resource::["${a}/contact/*",%s]' % inst:
                "Contact attributes are read for contacts of this instance; contact IDs are not known in advance.",
            'Resource::["${a}/*",%s]' % inst:
                "Scoped to child resources (users, flows, queues, integrations) of this Connect instance.",
            'Resource::["${a}/agent/*",%s]' % inst: "Scoped to users of this Connect instance.",
            'Resource::["${a}/queue/*",%s]' % inst: "Scoped to queues of this Connect instance.",
            'Resource::["${d}/*",{"d":{"Fn::GetAtt":["CasesDomain","DomainArn"]}}]':
                "Scoped to cases, templates and fields of this stack's Cases domain.",
            'Resource::["${d}/*",{"d":{"Fn::Sub":["arn:aws:profile:${AWS::Region}:${AWS::AccountId}:domains/${d}",'
            '{"d":"amazon-connect-%s"}]}}]' % self.node.try_get_context("instance_alias"):
                "Scoped to profiles of this stack's Customer Profiles domain.",
            "Action::s3:Abort*": "Granted by Bucket.grant_put for multipart uploads of the ringback prompt object.",
            "Resource::<PromptsBucket204FD37A.Arn>/*": "The helper writes the ringback prompt object into the prompts bucket.",
            "Resource::<ConnectHelperFn1D8B38D2.Arn>:*": "The CDK provider framework invokes versions of its handler.",
            "Resource::arn:aws:iam::<AWS::AccountId>:role/aws-service-role/connect.amazonaws.com/*":
                "Amazon Connect updates its own service-linked role when the Cases domain is associated or removed.",
            "Resource::arn:aws:lambda:<AWS::Region>:<AWS::AccountId>:event-source-mapping:*":
                "The VMX event source mapping UUID is only known after the VMX stack is created.",
        }
        for finding, reason in wildcard_findings.items():
            ack(self, "AwsSolutions-IAM5[%s]" % finding, reason)
