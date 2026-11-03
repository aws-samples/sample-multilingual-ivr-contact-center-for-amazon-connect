# Multilingual IVR Contact Center on Amazon Connect — Deployment and Operations Guide

**🌐 Language / 语言:** **English** · [简体中文](operations-manual.zh-CN.md)

This guide is for anyone deploying the system for the first time. Follow the steps in order to deploy and verify it. Each step gives the command to run, the expected result, and what to do if the result differs.

---

## Contents

0. What you get
1. Before you begin
2. Install the tools
3. Configure AWS credentials
4. Get the code
5. Edit the deployment configuration
6. Deploy
7. After deployment: phone number, passwords, sign-in
8. Acceptance tests
9. Making changes
10. Troubleshooting
11. Delete all resources
Appendix A: Resources created
Appendix B: Deployment parameters

---

## 0. What you get

One deployment command (about 7–8 minutes) gives you a complete, working phone contact center in your AWS account:

| Capability | Description |
|---|---|
| Inbound number | A US DID number is claimed and associated with the inbound flow `ivr-main` |
| Language menu | Mandarin / Cantonese / English / Spanish; the selected language is stored as a contact attribute |
| Caller identification | The caller's number is matched to a customer profile (or a new one is created), which opens automatically when the agent answers |
| Cases | Agents can create cases for the current caller; a new case is assigned to the agent who created it, and agents see only their own cases |
| Returning callers | When a caller with an open case calls again, the answering agent hears the case number and owner first; if the owner is available, the call is offered to the owner first for 20 seconds |
| Unanswered calls | If no agent is available, the caller hears about 30 seconds of ringback and is then offered voicemail; the voicemail gets a 6-digit ticket number and a transcript and is delivered to agents as a task |
| Supervisor | Real-time metrics, monitoring and barge-in on live calls, and access to all cases with assignment to any agent |
| Recording and transcription | Calls are recorded and transcribed in the selected language; recordings and transcripts are encrypted |

---

## 1. Before you begin

### 1.1 AWS account and permissions

- An AWS account.
- An IAM user or role that can bootstrap CDK and deploy through the CDK bootstrap roles. AWS CDK does not create resources with your own credentials: `cdk deploy` assumes the deployment role created by `cdk bootstrap`, and CloudFormation creates the resources with the bootstrap CloudFormation execution role. Your identity therefore needs `sts:AssumeRole` on the `cdk-*` bootstrap roles, plus the permissions to run `cdk bootstrap` once (CloudFormation, IAM role creation, S3, ECR and SSM Parameter Store).
- Grant the CloudFormation execution role only the permissions this stack needs. By default `cdk bootstrap` attaches `AdministratorAccess` to it; to apply least privilege, create a customer managed policy limited to the services the stack uses (Amazon Connect, Customer Profiles, Cases, AWS Lambda, IAM, Amazon S3, AWS KMS, Kinesis Data Streams, Amazon EventBridge, Secrets Manager, CloudWatch Logs and CloudFormation) and pass it at bootstrap, as shown in step 6.2.
- Deploy this sample into a dedicated account used only for evaluation, separate from production workloads.

### 1.2 Region

The default Region is **US East (N. Virginia), us-east-1**. To deploy to another Region, read Q8 in section 10 first.

### 1.3 Quota check

By default an AWS account can have at most **2** Amazon Connect instances per Region. Before deploying, confirm that the target Region has fewer than 2:

```bash
aws connect list-instances --region us-east-1 --query "length(InstanceSummaryList)"
```

**Expected result:** `0` or `1`. If the output is `2`, delete an instance you no longer use, or request an increase of the "Amazon Connect instance count" quota in the Service Quotas console.

### 1.4 Cost

| Item | Pricing |
|---|---|
| Phone number | Monthly fee for a US DID number (about USD 1 per month) |
| Calls | Per call minute (Amazon Connect, Amazon Transcribe, Contact Lens) |
| KMS customer managed key | About USD 1 per month |
| Other | Lambda, S3, Kinesis and other services are charged by usage and cost little at test volume |

When you no longer need the system, delete all resources as described in section 11; the phone number is released and billing stops.

---

## 2. Install the tools

The tools below work on macOS, Windows and Linux. After installing each one, run its verification command to confirm the version.

| Tool | Version | Purpose |
|---|---|---|
| AWS CLI | v2 | Access AWS |
| Node.js | 20, 22 or 24 (22 LTS recommended) | Runs the AWS CDK |
| Python | 3.11 or later | CDK app code |
| Git | Any | Get the code (not needed if you use a code archive) |

> **Note:** Do not use Node.js 26 or later; the jsii runtime used by the AWS CDK does not support it yet.

### 2.1 AWS CLI

Install: see https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html

Verify:

```bash
aws --version
```

**Expected result:** starts with `aws-cli/2.`, for example `aws-cli/2.31.0 Python/3.13 ...`.

### 2.2 Node.js

Install: download the 22 LTS installer from https://nodejs.org/, or run `brew install node@22` on macOS.

Verify:

```bash
node --version
```

**Expected result:** starts with `v20.`, `v22.` or `v24.`.

### 2.3 AWS CDK CLI

```bash
npm install -g aws-cdk
cdk --version
```

**Expected result:** starts with `2.`, for example `2.1031.0 (build ...)`.

> On macOS or Linux, if you get a permission error (`EACCES`), prefix the command with `sudo`, or use `npx aws-cdk@2` in place of `cdk` in the commands below.

### 2.4 Python

Install: download 3.11 or later from https://www.python.org/downloads/, or run `brew install python@3.12` on macOS.

Verify:

```bash
python3 --version
```

**Expected result:** `Python 3.11.x` or later.

> On Windows the command is `python` instead of `python3`; substitute it in the commands below.

---

## 3. Configure AWS credentials

### 3.1 Configure

Choose one method:

- **Access keys:** run `aws configure` and enter the Access Key ID, Secret Access Key, default Region `us-east-1` and output format `json`.
- **IAM Identity Center (SSO):** run `aws configure sso` and sign in when prompted; then run `export AWS_PROFILE=<profile-name>` (Windows: `set AWS_PROFILE=<profile-name>`) before the remaining commands.

### 3.2 Verify

```bash
aws sts get-caller-identity
```

**Expected result:** the output contains `"Account": "<12-digit account ID>"`, and it is the account you want to deploy to.

**If you see `Unable to locate credentials` or `ExpiredToken`:** the credentials are missing or expired; repeat 3.1.

Note the 12-digit account ID in the output; you need it in step 6.

---

## 4. Get the code

Clone the repository, or download and extract the code archive:

```bash
git clone <repository-url> connect-ivr
cd connect-ivr
```

If you received an archive, extract it and open a terminal in the extracted folder.

Verify:

```bash
ls
```

**Expected result:** you see folders such as `cdk`, `flows`, `lambdas` and `docs`.

> All later commands run in this project folder or in its `cdk` subfolder, as noted for each command.

---

## 5. Edit the deployment configuration

Open `cdk/cdk.json` in a text editor and change the following entries under `context`. Leave everything else unchanged.

### 5.1 Instance alias (required)

```json
"instance_alias": "<unique-instance-alias>",
```

Replace `<unique-instance-alias>` with your instance alias, for example `acme-contact-center`.

- The alias must be **globally unique across all AWS accounts**; an alias already in use cannot be deployed.
- Use only **lowercase letters, numbers and hyphens (-)**, 1–45 characters, starting and ending with a letter or number, and not starting with `d-`.
- The alias also sets the login URL: `https://<instance-alias>.my.connect.aws/`.

> If you deploy without changing it, the app stops with "Set instance_alias in cdk.json to a globally unique instance alias" and creates no resources.

### 5.2 Agents and supervisor (optional)

```json
"agents": [
  {"username": "agent1", "first_name": "Agent", "last_name": "One"},
  {"username": "agent2", "first_name": "Agent", "last_name": "Two"},
  {"username": "agent3", "first_name": "Agent", "last_name": "Three"}
],
"supervisor": {"username": "supervisor", "first_name": "Supervisor", "last_name": "Lead"},
```

- One line per agent: `username` is the sign-in name, `first_name` and `last_name` are the display name.
- Add or remove lines for as many agents as you need. Separate lines with commas, with no comma after the last line.
- Use only letters, numbers, `-`, `_` and `.` in `username`, and do not repeat a name.

### 5.3 Other parameters (usually unchanged)

| Parameter | Default | Description |
|---|---|---|
| `stack_name` | `ConnectIvrStack` | CloudFormation stack name. Use a different name for each copy deployed in the same account |
| `region` | `us-east-1` | Deployment Region |
| `claim_phone_number` | `true` | Whether to claim a phone number. With `false` no number is claimed (no monthly fee, but no inbound calls) |
| `phone_country_code` | `US` | Country code of the phone number |
| `owner_wait_seconds` | `20` | How long a returning caller is offered to the case owner first, in seconds |
| `cases_labels` | Chinese names | Display names of the Cases fields, layout and templates; see [README section 3](../README.md#3-deployment-parameters) for an English example |

### 5.4 Check the file format

In the project folder, run:

```bash
python3 -c "import json; json.load(open('cdk/cdk.json')); print('cdk.json is valid')"
```

**Expected result:** `cdk.json is valid`. If you get an error, it is usually a missing or extra comma or an unmatched quote; fix it at the line number shown.

---

## 6. Deploy

### 6.1 Install dependencies (once, before the first deployment)

```bash
cd cdk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> On Windows, the third line is `.venv\Scripts\activate`.

**Expected result:** the last line shows `Successfully installed ...`, and the prompt starts with `(.venv)`.

> Every time you open a new terminal for deployment commands, go to the `cdk` folder and run `source .venv/bin/activate` first.

### 6.2 Bootstrap CDK (once per account and Region)

Replace `<account-id>` with the 12-digit account ID from step 3.2:

```bash
cdk bootstrap aws://<account-id>/us-east-1
```

To limit the CloudFormation execution role to a customer managed policy instead of the default `AdministratorAccess` (recommended outside test accounts):

```bash
cdk bootstrap aws://<account-id>/us-east-1 \
  --cloudformation-execution-policies arn:aws:iam::<account-id>:policy/<deployment-policy-name>
```

> `cdk bootstrap` runs the CDK app, so `instance_alias` must already be set. If you pass it on the command line instead of editing `cdk.json`, pass it to `cdk bootstrap` as well, for example `cdk bootstrap aws://<account-id>/us-east-1 -c instance_alias=acme-contact-center`.

**Expected result:** the last line is `✅  Environment aws://<account-id>/us-east-1 bootstrapped.`. If the account and Region were bootstrapped before, you see `(no changes)`, which is also a success.

### 6.3 Pre-deployment check (recommended)

```bash
cdk synth > /dev/null && echo "Check passed"
```

> On Windows, run `cdk synth > NUL && echo Check passed`.

**Expected result:** `Check passed`. This command only generates the deployment template locally and runs the security and compliance checks; it creates no resources.

### 6.4 Deploy

```bash
cdk deploy --require-approval never
```

Deployment takes about **7–8 minutes**. The terminal shows resource creation progress (for example `45/1xx | CREATE_COMPLETE | AWS::Connect::Queue ...`); no action is needed while it runs.

**Expected result:** the output ends with:

```
 ✅  ConnectIvrStack

✨  Deployment time: 4xx.xxs

Outputs:
ConnectIvrStack.OutAgent1Password = arn:aws:secretsmanager:...
ConnectIvrStack.OutCasesDomainId = ...
ConnectIvrStack.OutInstanceId = ...
ConnectIvrStack.OutLoginUrl = https://<instance-alias>.my.connect.aws/
ConnectIvrStack.OutMediaBucket = ...
ConnectIvrStack.OutPhoneNumber = +1XXXXXXXXXX
ConnectIvrStack.OutSupervisorPassword = arn:aws:secretsmanager:...
```

**Save the complete Outputs section**; later steps use it.

**If the deployment fails:** the terminal shows a `CREATE_FAILED` line with the reason, and the created resources are rolled back automatically. Resolve the issue using section 10, then run this step again.

---

## 7. After deployment: phone number, passwords, sign-in

### 7.1 Inbound number

`OutPhoneNumber` in the Outputs is the inbound number. It is already associated with the inbound flow `ivr-main` and can be called as soon as deployment completes.

### 7.2 Get the sign-in passwords

Each agent's and the supervisor's password is generated at deployment and stored in AWS Secrets Manager. `OutAgent1Password`, `OutSupervisorPassword` and the other password outputs are the ARNs where the passwords are stored.

| Output | User |
|---|---|
| `OutAgent1Password` | First agent in the `agents` list |
| `OutAgent2Password` | Second agent, and so on |
| `OutSupervisorPassword` | Supervisor |

To get a password, replace `<ARN>` with the corresponding output value:

```bash
aws secretsmanager get-secret-value --secret-id <ARN> --query SecretString --output text
```

**Expected result:** a 16-character password of uppercase letters, lowercase letters and digits.

> Passwords are sensitive; do not send them in plain text by chat or email.

### 7.3 Sign in

1. Open `OutLoginUrl` from the Outputs (`https://<instance-alias>.my.connect.aws/`) in a browser; the latest Chrome or Edge is recommended.
2. Enter the user name (for example `agent1`) and the password from 7.2.
3. **Agents:** open the Agent Workspace. On first use the browser asks for microphone access; choose Allow. Set your status to **Available** to receive calls.
4. **Supervisor:** after sign-in, use the left menu to open real-time metrics, Cases and other pages.

> **Network requirement:** the agent softphone carries audio over WebRTC, which some corporate networks restrict, causing silence after the call connects. If this happens, switch to a network that allows WebRTC and try again.

---

## 8. Acceptance tests

Run the five tests below in order. Each lists the steps and the expected result; if all pass, the deployment is working.

### 8.1 Answered call: customer profile and case

1. Sign in as agent1 to the Agent Workspace and set the status to Available.
2. Call the inbound number from a mobile phone and press any language key at the menu (for example 1 for Mandarin).
3. agent1 answers.

**Expected result:**
- After the call connects, the customer profile appears on the right of the Agent Workspace (a new profile is created for a first-time number).
- In the Agent Workspace choose **Actions → Create Case**, select agent1's template (default name `工单-agent1`), enter a title and save. The case's Assigned User is set to agent1 automatically.

> Agents must create cases with their own template (`<agent_template_prefix><username>`, where the prefix comes from the `cases_labels` deployment parameter). Choosing another agent's template is denied by access control.
- About 1–2 minutes after hanging up, the recording appears under `recordings/` in the S3 media bucket (`OutMediaBucket` in the Outputs), and the transcript under `transcripts/`.

### 8.2 Per-agent case isolation

Sign in as agent2, open Cases and search.

**Expected result:** agent2 cannot see the case agent1 created in 8.1.

### 8.3 Supervisor assignment and reassignment

1. Sign in as supervisor, open Cases and find the case created in 8.1.
2. Change Assigned User to agent2 and save.

**Expected result:**
- The supervisor can see and edit the case.
- After refreshing, agent2 can see the case and agent1 no longer can.
- When done, change Assigned User back to agent1 for 8.4.

### 8.4 Returning caller routed to the case owner

1. Set both agent1 and agent2 to Available.
2. Call the inbound number again from the phone used in 8.1 and select a language.

**Expected result:**
- The call is offered to agent1 (the case owner) first.
- When agent1 accepts, before talking to the caller, agent1 hears "This caller has an open case, number …, owned by agent1".
- If agent1 does not accept within 20 seconds, the call goes to agent2; if agent1 is Offline or in any status other than Available, the call goes to agent2 immediately.

### 8.5 Unanswered call: ringback and voicemail

1. Set all agents to Offline.
2. Call the inbound number and select a language.
3. After the hold message and about 30 seconds of ringback, the system offers voicemail.
4. Press 1, listen to the 6-digit ticket number and the tone, leave a message, press **#**, and hang up.
5. Set any agent to Available.

**Expected result:**
- About 15–30 seconds after hanging up, the agent receives an "Amazon Connect Voicemail" task in which the recording can be played.
- In Cases you can find the matching case, with the 6-digit ticket number and the voicemail transcript, linked to the caller's customer profile.
- The voicemail case is visible to all agents.

---

## 9. Making changes

All changes are made by editing `cdk/cdk.json` and redeploying. A redeployment updates only what changed and does not affect existing cases, customer profiles or recordings.

```bash
cd cdk
source .venv/bin/activate
cdk deploy --require-approval never
```

| Change | How |
|---|---|
| Add an agent | Add a line to `agents` and redeploy. Get the new agent's password from the new `OutAgent<N>Password` output |
| Remove an agent | Remove the line from `agents` and redeploy. The agent's user, security profile and quick connect are deleted, and so is their template; their cases remain and the supervisor can reassign them |
| Change an agent's display name | Change `first_name` / `last_name` on the agent's line and redeploy |
| Change the returning-caller wait | Change `owner_wait_seconds` and redeploy |
| Change Cases display names | Change `cases_labels` and redeploy; Cases resources are updated in place |
| Change an agent's password | The agent changes it in their profile settings, or an administrator resets it under user management on the Amazon Connect admin website |

> **Do not change the `username` of a deployed agent.** Changing it is treated as deleting the old agent and creating a new one.

> **Preview a change before deploying:** run `cdk diff` to see which resources the deployment will add, modify or delete, without changing anything.

---

## 10. Troubleshooting

**Q1 The deployment reports that the instance alias already exists or is unavailable**
The alias is already in use, possibly by another AWS account. Change `instance_alias` in `cdk/cdk.json` and redeploy.

**Q2 The deployment says bootstrap is required (`This stack uses assets ... run cdk bootstrap`)**
Step 6.2 was not run, or the deployment Region differs from the bootstrapped Region. Run 6.2 and try again.

**Q3 The `cdk` command fails with an unsupported Node.js version**
Install Node.js 22 LTS as described in step 2.2.

**Q4 The deployment reports that the instance limit is exceeded**
The target Region already has 2 Amazon Connect instances; see step 1.3.

**Q5 Claiming the phone number fails**
No numbers are available for the country, or the number quota is reached. Set `claim_phone_number` to `false` to finish the deployment, then claim a number under Channels → Phone numbers on the Amazon Connect admin website and select `ivr-main` as its contact flow.

**Q6 After a failed deployment rolls back, redeploying reports that a resource already exists**
Some resources may not have been deleted during rollback. Check for a leftover Amazon Connect instance with the same alias:

```bash
aws connect list-instances --query "InstanceSummaryList[].[InstanceAlias,Id]" --output text
```

If an instance with this deployment's alias is listed, confirm that it is left over from the failed deployment (not an instance in use), then delete it:

```bash
aws connect delete-instance --instance-id <instance-id>
```

If the stack status in the CloudFormation console is `ROLLBACK_COMPLETE` or `DELETE_FAILED`, delete the stack first (for `DELETE_FAILED`, choose to retain the resources that cannot be deleted), then redeploy.

**Q7 `cdk synth` or `cdk deploy` fails with an error starting with `AwsSolutions-`**
A security and compliance check failed, usually because the permission configuration in the code was changed. Revert the permission change; if the change is required, record each reviewed and accepted finding with its reason in `ConnectIvrStack._acknowledge_nag_findings`.

**Q8 Can I deploy to another Region?**
Yes. Change `region` in `cdk.json` and run 6.2 for that Region. The Region must support Amazon Connect, Customer Profiles, Cases and Amazon Transcribe, and host the official Voicemail Express (VMX) template. Validate in a test account first.

**Q9 The agent hears nothing after accepting a call**
See the network requirement in step 7.3, and check that the browser allows microphone access.

**Q10 The agent does not receive a voicemail task**
1. Confirm that the caller pressed **#** before hanging up (the voicemail is not saved otherwise).
2. Confirm that at least one agent is Available.
3. In CloudWatch Logs, open the log group whose name contains `VoicemailFastTriggerFn` and check for a recent `invoked RecordingProcessor` entry.

**Q11 A returning call is not offered to the case owner**
1. Confirm that the caller has a case with status Open and an assignee.
2. Confirm that the owner is Available (offline, break and other statuses do not wait).
3. Confirm that the call comes from the same number (callers are identified by their phone number).

---

## 11. Delete all resources

> **Warning:** Deletion cannot be undone. The following are permanently deleted: call recordings, transcripts, customer profiles, cases, and the Amazon Connect instance with all its configuration. The phone number is released and cannot be recovered.

```bash
cd cdk
source .venv/bin/activate
cdk destroy
```

When asked `Are you sure you want to delete: ConnectIvrStack (y/n)?`, enter `y` and press Enter. Deletion takes about 10 minutes.

**Expected result:** the output ends with `✅  ConnectIvrStack: destroyed`.

The Lambda log groups are deleted with the stack. Log groups created by the VMX nested stack's functions may remain (their cost is low); to remove them, search for the instance alias under Log groups in the CloudWatch console and delete them.

---

## Appendix A: Resources created

| Category | Resources |
|---|---|
| Encryption and storage | KMS customer managed key; S3 media bucket (recordings and transcripts); S3 prompts bucket; S3 access log bucket |
| Amazon Connect | Instance; recording storage configuration; contact record stream; hours of operation `always-open`; queues `main` and `case-assignment`; routing profiles `agent-rp` (agents) and `supervisor-rp` (supervisor); security profiles `Agent-<username>`, `Agent-voicemail-pool` and `Supervisor`; agent and supervisor users; quick connects; ringback prompt `Ringback_CN_450`; contact flows `ivr-main`, `ivr-customer-queue` and `agent-whisper-case-notice`; phone number |
| Customer Profiles | Domain `amazon-connect-<instance-alias>`; contact record object type; integration with the instance |
| Cases | Domain `<instance-alias>-cases`; ticket number and voicemail transcript fields; standard layout; standard, voicemail and per-agent templates (names set by `cases_labels`); event stream; association with the instance |
| Lambda | Call transcription; voicemail case; voicemail transcript backfill; returning-caller routing; case assignment; voicemail fast trigger; deployment helper (each with a log group retained for 1 month) |
| EventBridge | Voicemail transcript backfill rule (VMX transcripts bucket); voicemail fast trigger rule; case assignment rule |
| Voicemail Express | Official VMX V3 nested stack (voicemail recording, transcription and task delivery) |
| Other | Kinesis Data Streams (contact records); Secrets Manager (user passwords) |

## Appendix B: Deployment parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `instance_alias` | String | Yes | Amazon Connect instance alias, globally unique; also the resource name prefix and part of the login URL |
| `agents` | List | Yes | Agent list; each entry has `username`, `first_name` and `last_name` |
| `supervisor` | Object | Yes | Supervisor with `username`, `first_name` and `last_name` |
| `stack_name` | String | No | CloudFormation stack name, default `ConnectIvrStack` |
| `region` | String | No | Deployment Region, default `us-east-1` |
| `claim_phone_number` | Boolean | No | Whether to claim a phone number, default `true` |
| `phone_country_code` | String | No | Country code of the phone number, default `US` |
| `owner_wait_seconds` | Integer | No | How long a returning caller is offered to the case owner first, in seconds, default `20` |
| `cases_labels` | Object | No | Cases display names, Chinese by default; set only the keys you want to change |

Parameters can also be set on the command line, for example:

```bash
cdk deploy -c instance_alias=acme-contact-center -c stack_name=AcmeContactCenter
```
