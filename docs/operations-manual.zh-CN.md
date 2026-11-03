# Amazon Connect 多语言 IVR 联络中心 —— 部署与操作手册

**🌐 Language / 语言：** [English](operations-manual.md) · **简体中文**

本手册面向首次部署本系统的人员，按顺序逐步执行即可完成部署与验收。每一步都给出要执行的命令、预期结果，以及结果不符时的处理方法。

---

## 目录

0. 部署完成后得到什么
1. 准备工作
2. 安装本机工具
3. 配置 AWS 凭证
4. 获取项目代码
5. 修改部署配置
6. 部署
7. 部署后操作：号码、密码、登录
8. 功能验收
9. 日常变更
10. 常见问题
11. 删除全部资源
附录 A：部署创建的资源清单
附录 B：部署参数说明

---

## 0. 部署完成后得到什么

执行一次部署命令（约 7–8 分钟），即可在 AWS 账号中获得一套完整可用的电话联络中心：

| 能力 | 说明 |
|---|---|
| 呼入号码 | 自动申领一个美国 DID 号码，并关联呼入联系流 `ivr-main` |
| 语言菜单 | 普通话 / 粤语 / 英语 / 西班牙语，所选语言作为联系人标签 |
| 客户识别 | 按主叫号码自动匹配或新建客户资料，座席接听时自动弹出 |
| 工单 | 座席可为当前客户创建工单，工单自动指派给创建座席；座席只能看到本人的工单 |
| 回访客户 | 存在未结工单的客户再次来电时，座席接听前收到工单号与负责人提示；负责人在线时优先分配给负责人 20 秒 |
| 未接听处理 | 无可用座席时播放回铃音约 30 秒，之后提示客户留言；留言自动生成 6 位工单号、写入转写内容，并以任务形式分配给座席 |
| 主管 | 可查看实时指标、监听与插入通话、查看全部工单并指派给任一座席 |
| 录音与转写 | 通话全程录音，按所选语言自动转写，录音与转写结果加密存储 |

---

## 1. 准备工作

### 1.1 AWS 账号与权限

- 一个 AWS 账号。
- 一个可以执行 CDK bootstrap、并通过 CDK bootstrap 角色部署的 IAM 用户或角色。AWS CDK 不使用部署者自身的凭证创建资源：`cdk deploy` 承担 `cdk bootstrap` 创建的部署角色，再由 CloudFormation 使用 bootstrap 创建的 CloudFormation 执行角色创建资源。因此部署者需要对 `cdk-*` bootstrap 角色的 `sts:AssumeRole` 权限，以及首次执行 `cdk bootstrap` 所需的权限（CloudFormation、IAM 角色创建、S3、ECR、SSM Parameter Store）。
- CloudFormation 执行角色应仅授予本堆栈所需的权限。`cdk bootstrap` 默认为其附加 `AdministratorAccess`；如需遵循最小权限原则，可创建仅包含本堆栈所用服务（Amazon Connect、Customer Profiles、Cases、AWS Lambda、IAM、Amazon S3、AWS KMS、Kinesis Data Streams、Amazon EventBridge、Secrets Manager、CloudWatch Logs、CloudFormation）权限的客户托管策略，并在 bootstrap 时指定，见第 6.2 步。
- 建议将本示例部署在仅用于评估的独立账号中，与生产工作负载隔离。

### 1.2 区域

默认部署在 **美国东部（弗吉尼亚北部）us-east-1**。如需部署到其他区域，请先阅读第 10 节"Q8 能否部署到其他区域"。

### 1.3 配额检查

每个 AWS 账号在每个区域默认最多创建 **2 个** Amazon Connect 实例。部署前确认目标区域现有实例数少于 2：

```bash
aws connect list-instances --region us-east-1 --query "length(InstanceSummaryList)"
```

**预期结果：** 输出 `0` 或 `1`。如果输出 `2`，需要先删除一个不再使用的实例，或在 Service Quotas 控制台申请提高 "Amazon Connect instance count" 配额。

### 1.4 费用

| 项目 | 计费方式 |
|---|---|
| 电话号码 | 美国 DID 号码按月计费（约 1 美元 / 月） |
| 通话 | 按通话分钟计费（Amazon Connect、Amazon Transcribe、Contact Lens） |
| KMS 客户托管密钥 | 约 1 美元 / 月 |
| 其他 | Lambda、S3、Kinesis 等按用量计费，测试规模下费用很低 |

不再使用时，请按第 11 节删除全部资源，号码会随之释放、停止计费。

---

## 2. 安装本机工具

以下工具在 macOS、Windows、Linux 上均可使用。每个工具安装后都请执行验证命令，确认版本符合要求。

| 工具 | 版本要求 | 用途 |
|---|---|---|
| AWS CLI | v2 | 访问 AWS |
| Node.js | 20、22 或 24（推荐 22 LTS） | 运行 AWS CDK |
| Python | 3.11 或以上 | CDK 应用代码 |
| Git | 任意版本 | 获取代码（如使用代码包可不安装） |

> **注意：** Node.js 请不要使用 26 及以上版本，AWS CDK 依赖的 jsii 运行时尚不支持。

### 2.1 AWS CLI

安装：参照 https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html

验证：

```bash
aws --version
```

**预期结果：** 以 `aws-cli/2.` 开头，例如 `aws-cli/2.31.0 Python/3.13 ...`。

### 2.2 Node.js

安装：从 https://nodejs.org/ 下载 22 LTS 版本安装包；或在 macOS 上执行 `brew install node@22`。

验证：

```bash
node --version
```

**预期结果：** 以 `v20.`、`v22.` 或 `v24.` 开头。

### 2.3 AWS CDK CLI

```bash
npm install -g aws-cdk
cdk --version
```

**预期结果：** 以 `2.` 开头，例如 `2.1031.0 (build ...)`。

> macOS / Linux 如果提示权限不足（`EACCES`），在命令前加 `sudo`，或改用 `npx aws-cdk@2` 代替下文中的 `cdk` 命令。

### 2.4 Python

安装：从 https://www.python.org/downloads/ 下载 3.11 或以上版本；或在 macOS 上执行 `brew install python@3.12`。

验证：

```bash
python3 --version
```

**预期结果：** `Python 3.11.x` 或更高。

> Windows 上命令为 `python` 而不是 `python3`，下文同理替换。

---

## 3. 配置 AWS 凭证

### 3.1 配置

任选一种方式：

- **访问密钥：** 执行 `aws configure`，依次输入 Access Key ID、Secret Access Key、默认区域 `us-east-1`、输出格式 `json`。
- **IAM Identity Center（SSO）：** 执行 `aws configure sso` 并按提示登录；之后先执行 `export AWS_PROFILE=<配置名>`（Windows：`set AWS_PROFILE=<配置名>`），再执行后续命令。

### 3.2 验证

```bash
aws sts get-caller-identity
```

**预期结果：** 输出包含 `"Account": "<12 位账号 ID>"`，且该账号就是要部署的账号。

**如果报错 `Unable to locate credentials` 或 `ExpiredToken`：** 凭证未配置或已过期，重新执行 3.1。

记下输出中的 12 位账号 ID，第 6 步要用。

---

## 4. 获取项目代码

克隆本项目的代码仓库，或下载并解压代码包：

```bash
git clone <仓库地址> connect-ivr
cd connect-ivr
```

如果收到的是压缩包，解压后在终端进入解压出的目录。

验证：

```bash
ls
```

**预期结果：** 可以看到 `cdk`、`flows`、`lambdas`、`docs` 等文件夹。

> 后续所有命令都在这个项目目录或其下的 `cdk` 目录中执行，命令中会注明。

---

## 5. 修改部署配置

用任意文本编辑器打开 `cdk/cdk.json`，修改 `context` 中的以下内容，其余内容不要改动。

### 5.1 实例别名（必改）

```json
"instance_alias": "<unique-instance-alias>",
```

将 `<unique-instance-alias>` 替换为实例别名，例如 `acme-contact-center`。

- 别名在**所有 AWS 账号中全局唯一**，已被使用的别名无法部署。
- 只能包含**小写字母、数字和连字符（-）**，长度 1–45 个字符，以字母或数字开头和结尾，不能以 `d-` 开头。
- 别名同时决定登录网址：`https://<实例别名>.my.connect.aws/`。

> 不修改直接部署时，程序会提示 "Set instance_alias in cdk.json to a globally unique instance alias" 并停止，不会创建任何资源。

### 5.2 座席与主管（按需修改）

```json
"agents": [
  {"username": "agent1", "first_name": "Agent", "last_name": "One"},
  {"username": "agent2", "first_name": "Agent", "last_name": "Two"},
  {"username": "agent3", "first_name": "Agent", "last_name": "Three"}
],
"supervisor": {"username": "supervisor", "first_name": "Supervisor", "last_name": "Lead"},
```

- 每个座席一行：`username` 是登录用户名，`first_name` / `last_name` 是显示名称。
- 需要几名座席就写几行，增加或删除行即可。行与行之间用英文逗号分隔，最后一行后面不加逗号。
- `username` 只使用字母、数字、`-`、`_`、`.`，且不能重复。

### 5.3 其他参数（一般不用修改）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `stack_name` | `ConnectIvrStack` | CloudFormation 堆栈名称。同一账号部署多套时改为不同名称 |
| `region` | `us-east-1` | 部署区域 |
| `claim_phone_number` | `true` | 是否自动申领号码。设为 `false` 则不申领（不产生号码月租，但无法接听电话） |
| `phone_country_code` | `US` | 号码国家代码 |
| `owner_wait_seconds` | `20` | 回访客户优先分配给原负责座席的等待秒数 |
| `cases_labels` | 中文名称 | Cases 字段、布局与模板的显示名称，可改为英文等，见 [README 第 3 节](../README.zh-CN.md#3-部署参数) |

### 5.4 检查配置格式

在项目目录执行：

```bash
python3 -c "import json; json.load(open('cdk/cdk.json')); print('cdk.json 格式正确')"
```

**预期结果：** 输出 `cdk.json 格式正确`。如果报错，通常是漏了逗号、多了逗号或引号不成对，按报错提示的行号修正。

---

## 6. 部署

### 6.1 安装依赖（首次部署执行一次）

```bash
cd cdk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> Windows 上第三行改为 `.venv\Scripts\activate`。

**预期结果：** 最后一行出现 `Successfully installed ...`，且终端提示符前出现 `(.venv)`。

> 以后每次打开新终端执行部署相关命令，都要先进入 `cdk` 目录并执行 `source .venv/bin/activate`。

### 6.2 初始化 CDK（每个账号每个区域执行一次）

把 `<账号ID>` 替换为第 3.2 步记下的 12 位账号 ID：

```bash
cdk bootstrap aws://<账号ID>/us-east-1
```

如需将 CloudFormation 执行角色限定为客户托管策略、而非默认的 `AdministratorAccess`（测试账号以外的环境建议如此）：

```bash
cdk bootstrap aws://<账号ID>/us-east-1 \
  --cloudformation-execution-policies arn:aws:iam::<账号ID>:policy/<部署策略名称>
```

**预期结果：** 最后一行为 `✅  Environment aws://<账号ID>/us-east-1 bootstrapped.`。如果该账号区域之前已初始化过，会显示 `(no changes)`，同样表示成功。

### 6.3 部署前检查（推荐）

```bash
cdk synth > /dev/null && echo "检查通过"
```

> Windows 上执行 `cdk synth > NUL && echo 检查通过`。

**预期结果：** 输出 `检查通过`。该命令只在本地生成部署模板并执行安全合规检查，不会创建任何资源。

### 6.4 执行部署

```bash
cdk deploy --require-approval never
```

部署约需 **7–8 分钟**。期间终端会持续显示资源创建进度（例如 `45/1xx | CREATE_COMPLETE | AWS::Connect::Queue ...`），无需操作，等待完成即可。

**预期结果：** 最后出现：

```
 ✅  ConnectIvrStack

✨  Deployment time: 4xx.xxs

Outputs:
ConnectIvrStack.OutAgent1Password = arn:aws:secretsmanager:...
ConnectIvrStack.OutCasesDomainId = ...
ConnectIvrStack.OutInstanceId = ...
ConnectIvrStack.OutLoginUrl = https://<实例别名>.my.connect.aws/
ConnectIvrStack.OutMediaBucket = ...
ConnectIvrStack.OutPhoneNumber = +1XXXXXXXXXX
ConnectIvrStack.OutSupervisorPassword = arn:aws:secretsmanager:...
```

**请把 Outputs 部分完整复制保存**，后续步骤会用到。

**如果部署失败：** 终端会显示 `CREATE_FAILED` 行及原因，并自动回滚删除已创建的资源。按第 10 节常见问题处理后，重新执行本步骤。

---

## 7. 部署后操作：号码、密码、登录

### 7.1 呼入号码

Outputs 中的 `OutPhoneNumber` 即为呼入号码，已自动关联呼入联系流 `ivr-main`，部署完成即可拨打。

### 7.2 获取登录密码

每个座席和主管的密码在部署时自动生成，保存在 AWS Secrets Manager 中。Outputs 中的 `OutAgent1Password`、`OutSupervisorPassword` 等是对应密码的存放位置（ARN）。

| Outputs 名称 | 对应用户 |
|---|---|
| `OutAgent1Password` | `agents` 列表中的第 1 名座席 |
| `OutAgent2Password` | 第 2 名座席，依此类推 |
| `OutSupervisorPassword` | 主管 |

获取密码：把 `<ARN>` 替换为 Outputs 中对应的值：

```bash
aws secretsmanager get-secret-value --secret-id <ARN> --query SecretString --output text
```

**预期结果：** 输出 16 位由大小写字母和数字组成的密码。

> 密码属于敏感信息，请勿通过聊天工具或邮件明文传递。

### 7.3 登录

1. 浏览器打开 Outputs 中的 `OutLoginUrl`（`https://<实例别名>.my.connect.aws/`），推荐使用最新版 Chrome 或 Edge。
2. 输入用户名（如 `agent1`）和 7.2 获取的密码，登录。
3. **座席：** 进入 Agent Workspace（座席工作区）。首次使用时浏览器会请求麦克风权限，选择"允许"。将状态设为 **Available** 即可接听来电。
4. **主管：** 登录后可在左侧菜单进入实时指标、工单（Cases）等页面。

> **网络要求：** 座席软电话通过 WebRTC 传输语音，部分企业网络会限制该流量，导致接起后听不到声音。遇到这种情况请更换为允许 WebRTC 的网络后重试。

---

## 8. 功能验收

以下 5 项按顺序执行。每项给出操作步骤与预期结果，全部符合即表示部署成功。

### 8.1 已接听链路：客户资料与工单

1. agent1 登录 Agent Workspace，状态设为 Available。
2. 用手机拨打呼入号码，听到语言菜单后按任一语言键（如按 1 选择普通话）。
3. agent1 接听。

**预期结果：**
- 接通后，座席工作区右侧自动显示客户资料（首次来电的号码会自动新建客户资料）。
- 在座席工作区选择 **Actions → Create Case**，模板选择 `工单-agent1`，填写标题后保存；工单的 Assigned User 自动为 agent1。

> 座席创建工单时必须选择本人的模板（`工单-<本人用户名>`，名称前缀由部署参数 `cases_labels` 的 `agent_template_prefix` 决定）。选择其他模板会因访问控制被拒绝创建。
- 挂机后约 1–2 分钟，在 S3 媒体存储桶（Outputs 中的 `OutMediaBucket`）的 `recordings/` 下可看到录音，`transcripts/` 下可看到转写结果。

### 8.2 工单按座席隔离

agent2 登录，打开 Cases 页面并搜索。

**预期结果：** 看不到 agent1 在 8.1 创建的工单。

### 8.3 主管指派与转派

1. supervisor 登录，打开 Cases 页面，找到 8.1 创建的工单。
2. 将 Assigned User 改为 agent2 并保存。

**预期结果：**
- 主管可以看到并修改该工单。
- agent2 刷新后可以看到该工单；agent1 刷新后不再看到。
- 完成后将 Assigned User 改回 agent1，供 8.4 使用。

### 8.4 回访客户优先路由至原负责座席

1. agent1 与 agent2 均设为 Available。
2. 用 8.1 的同一部手机再次拨打呼入号码并选择语言。

**预期结果：**
- 呼叫优先分配给 agent1（工单负责人）。
- agent1 接起后、与客户通话前，先听到英语提示 "This caller has an open case, number …, owned by agent1"。
- 如果 agent1 在 20 秒内未接起，呼叫转由 agent2 接听；若 agent1 处于 Offline 等非 Available 状态，呼叫直接分配给 agent2。

### 8.5 未接听链路：回铃等候与语音留言

1. 所有座席设为 Offline。
2. 拨打呼入号码并选择语言。
3. 听到等候提示与约 30 秒回铃音后，系统提示按 1 留言。
4. 按 1，听到 6 位工单号播报与提示音后说一段话，说完按 **#**，然后挂机。
5. 任一座席设为 Available。

**预期结果：**
- 挂机后约 15–30 秒，座席收到 "Amazon Connect Voicemail" 任务，任务中可播放留言录音。
- 在 Cases 中可以找到对应工单：包含 6 位工单号与留言转写内容，并关联到该来电号码的客户资料。
- 该语音留言工单对所有座席可见。

---

## 9. 日常变更

所有变更都通过修改 `cdk/cdk.json` 后重新部署完成。重新部署只更新有变化的部分，不影响已有的工单、客户资料和录音。

```bash
cd cdk
source .venv/bin/activate
cdk deploy --require-approval never
```

| 变更 | 修改方法 |
|---|---|
| 增加座席 | 在 `agents` 中增加一行，重新部署。新座席的密码从 Outputs 中新增的 `OutAgent<N>Password` 获取 |
| 删除座席 | 从 `agents` 中删除对应行，重新部署。该座席的用户、安全配置文件与快速连接会被删除，其专属工单模板随之移除；其名下工单仍保留，可由主管转派 |
| 修改座席显示名称 | 修改对应行的 `first_name` / `last_name`，重新部署 |
| 修改回访等待时长 | 修改 `owner_wait_seconds`，重新部署 |
| 修改 Cases 显示名称 | 修改 `cases_labels`，重新部署；Cases 资源原地更新 |
| 修改座席密码 | 座席登录后在个人设置中修改，或由管理员在 Amazon Connect 管理网站的用户管理中重置 |

> **不要修改已部署座席的 `username`。** 修改用户名会被视为删除旧座席、新建新座席。

> **部署前预览变更：** 执行 `cdk diff`，可在不做任何改动的情况下查看本次部署将新增、修改、删除哪些资源。

---

## 10. 常见问题

**Q1 部署时提示实例别名已存在或不可用**
实例别名已被使用（可能是其他 AWS 账号）。修改 `cdk/cdk.json` 中的 `instance_alias` 后重新部署。

**Q2 部署时提示需要 bootstrap（`This stack uses assets ... run cdk bootstrap`）**
未执行第 6.2 步，或部署区域与 bootstrap 区域不一致。按 6.2 执行后重试。

**Q3 `cdk` 命令报错，提示 Node.js 版本不受支持**
Node.js 版本不符合要求，按第 2.2 步安装 22 LTS 版本。

**Q4 部署时提示实例数量超出限制**
目标区域已有 2 个 Amazon Connect 实例，见第 1.3 步。

**Q5 部署时号码申领失败**
目标国家暂无可用号码或号码配额不足。可先将 `claim_phone_number` 设为 `false` 完成部署，之后在 Amazon Connect 管理网站的 Channels → Phone numbers 中手动申领号码，并将 Contact flow 选择为 `ivr-main`。

**Q6 部署失败回滚后再次部署，提示资源已存在**
回滚过程中个别资源可能未被删除。执行以下命令检查是否残留同名 Amazon Connect 实例：

```bash
aws connect list-instances --query "InstanceSummaryList[].[InstanceAlias,Id]" --output text
```

若列出与本次别名相同的实例，确认它是本次部署失败遗留的（不是正在使用的实例）后删除：

```bash
aws connect delete-instance --instance-id <实例ID>
```

若 CloudFormation 控制台中堆栈状态为 `ROLLBACK_COMPLETE` 或 `DELETE_FAILED`，先删除该堆栈（`DELETE_FAILED` 时勾选保留无法删除的资源），再重新部署。

**Q7 `cdk synth` 或 `cdk deploy` 报 `AwsSolutions-` 开头的错误**
这是安全合规检查未通过，通常是修改了代码中的权限配置。请撤销对权限配置的修改；如确需调整，应在 `ConnectIvrStack._acknowledge_nag_findings` 中逐条记录经评估接受的检查项及理由。

**Q8 能否部署到其他区域**
可以。修改 `cdk.json` 中的 `region`，并在该区域执行 6.2。前提是该区域同时支持 Amazon Connect、Customer Profiles、Cases、Amazon Transcribe 与 Voicemail Express（VMX）官方模板，例如 us-west-2。部署前请先在测试账号验证。

**Q9 座席接起电话后听不到声音**
见第 7.3 步网络要求。另请检查浏览器是否已允许麦克风权限。

**Q10 留言后座席没有收到语音留言任务**
1. 确认挂机前按了 **#** 键（未按 # 时留言不会保存）。
2. 确认至少有一名座席处于 Available 状态。
3. 在 CloudWatch Logs 中打开名称包含 `VoicemailFastTriggerFn` 的日志组，确认最近是否有 `invoked RecordingProcessor` 记录。

**Q11 回访来电没有优先分配给原负责座席**
1. 确认该客户存在状态为 Open 且已指派负责人的工单。
2. 确认负责人处于 Available 状态（离线、休息等状态不会等待）。
3. 确认使用的是同一来电号码（客户识别依据主叫号码）。

---

## 11. 删除全部资源

> **警告：** 删除操作不可恢复。以下数据会被一并永久删除：通话录音、转写结果、客户资料、工单、Amazon Connect 实例及全部配置；电话号码会被释放，之后无法找回同一号码。

```bash
cd cdk
source .venv/bin/activate
cdk destroy
```

终端提示 `Are you sure you want to delete: ConnectIvrStack (y/n)?` 时输入 `y` 并回车。删除约需 10 分钟。

**预期结果：** 最后显示 `✅  ConnectIvrStack: destroyed`。

本堆栈创建的 Lambda 日志组随堆栈一并删除。VMX 嵌套堆栈中函数产生的日志组可能保留（费用很低），如需清理，在 CloudWatch 控制台的 Log groups 中搜索实例别名并删除。

---

## 附录 A：部署创建的资源清单

| 类别 | 资源 |
|---|---|
| 加密与存储 | KMS 客户托管密钥；S3 媒体存储桶（录音、转写）；S3 提示音存储桶；S3 访问日志存储桶 |
| Amazon Connect | 实例；录音存储配置；联系人记录流；营业时间 `always-open`；队列 `main` 与 `case-assignment`；路由配置文件 `agent-rp`（座席）、`supervisor-rp`（主管）；安全配置文件 `Agent-<用户名>`、`Agent-voicemail-pool`、`Supervisor`；座席与主管用户；快速连接；回铃音提示 `Ringback_CN_450`；联系流 `ivr-main`、`ivr-customer-queue`、`agent-whisper-case-notice`；电话号码 |
| Customer Profiles | 域 `amazon-connect-<实例别名>`；联系人记录对象类型；与实例的集成 |
| Cases | 域 `<实例别名>-cases`；字段"工单号""留言转写"；布局"标准工单布局"；模板"标准工单""语音留言工单""工单-<用户名>"；事件流；与实例的关联 |
| Lambda | 通话转写；语音留言工单；留言转写回写；回访客户路由；工单自动指派；语音留言快速处理；部署辅助函数（日志组保留 1 个月） |
| EventBridge | 留言转写回写规则（VMX 转写存储桶）；语音留言快速处理规则；工单自动指派规则 |
| Voicemail Express | 官方 VMX V3 嵌套堆栈（语音留言录制、转写、任务投递） |
| 其他 | Kinesis Data Streams（联系人记录）；Secrets Manager（各用户密码） |

## 附录 B：部署参数说明

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `instance_alias` | 字符串 | 是 | Amazon Connect 实例别名，全局唯一；同时作为资源名称前缀与登录网址 |
| `agents` | 列表 | 是 | 座席列表，每项包含 `username`、`first_name`、`last_name` |
| `supervisor` | 对象 | 是 | 主管，包含 `username`、`first_name`、`last_name` |
| `stack_name` | 字符串 | 否 | CloudFormation 堆栈名称，默认 `ConnectIvrStack` |
| `region` | 字符串 | 否 | 部署区域，默认 `us-east-1` |
| `claim_phone_number` | 布尔 | 否 | 是否自动申领号码，默认 `true` |
| `phone_country_code` | 字符串 | 否 | 号码国家代码，默认 `US` |
| `owner_wait_seconds` | 整数 | 否 | 回访客户优先分配给原负责座席的等待秒数，默认 `20` |
| `cases_labels` | 对象 | 否 | Cases 显示名称，默认中文；只需填写要修改的项 |

参数也可在部署命令中临时指定，例如：

```bash
cdk deploy -c instance_alias=acme-contact-center -c stack_name=AcmeContactCenter
```
