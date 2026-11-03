# IVR 菜单规格

**🌐 Language / 语言：** [English](MENU-SPEC.md) · **简体中文**

本文描述三个联系流模板的呼叫逻辑与按键定义。模板中的 `{{占位符}}` 由 CDK 在部署时替换为实际资源 ID（见 `cdk/stacks/connect_ivr_stack.py` 中的 `_flow_content`）。所有播报使用 Amazon Polly 语音 Joanna（英语）。

## 呼入联系流 `ivr-main.flow.json`

```
呼叫接入
  ├─ ① 客户识别：GetCustomerProfile（_phone = 主叫号码）
  │     ├─ 匹配到 → AssociateContactToCustomerProfile
  │     └─ 未匹配 → CreateCustomerProfile → AssociateContactToCustomerProfile
  │     └─ 写入联系人属性 vm_profile_id；任一步失败时跳过客户识别，继续后续流程
  ├─ ② 设置 VMX 流属性、播报语音 Joanna
  ├─ ③ 开启通话录音（座席 + 客户；IVR 阶段不录音）
  ├─ ④ 设置客户队列流 ivr-customer-queue
  ├─ ⑤ 合规提示："Thank you for calling. This call may be recorded for quality assurance."
  ├─ ⑥ 语言菜单（DTMF，等待 6 秒）
  │     ├─ 按 1 → language=mandarin，vmx3_lang=zh-CN
  │     ├─ 按 2 → language=cantonese，vmx3_lang=zh-HK
  │     ├─ 按 3 → language=english，vmx3_lang=en-US
  │     ├─ 按 4 → language=spanish，vmx3_lang=es-US
  │     └─ 超时或其他按键 → 按 3 处理（英语）
  ├─ ⑦ 设置目标队列 main（四个语言分支相同），写入 vmx3_queue_arn
  ├─ ⑧ 回访客户路由：调用 case-owner-lookup（失败时直接转入队列）
  │     ├─ 存在未结工单 → 设置座席耳语联系流 agent-whisper-case-notice
  │     └─ route_to_owner = true → UpdateContactRoutingCriteria（仅匹配负责人，owner_wait_seconds 后过期）
  └─ ⑨ 转入队列 main；转接失败或队列已满 → 播报 "Sorry, we are unable to take your call right now…" → 挂断
```

## 客户队列联系流 `customer-queue.flow.json`

基于 Voicemail Express（VMX）V3 的 `VMX3_In_Queue_Option` 联系流，在提供留言选项前增加回铃等候。

```
进入队列
  ├─ ① 播报 "Please hold while we connect you to an agent. Please stay on the line."
  ├─ ② 播放回铃音提示 Ringback_CN_450（约 30 秒）；期间座席可用时 Amazon Connect 中断播放并转接
  ├─ ③ 留言选项："…To leave a voicemail, press 1, or press any other key to remain in queue."
  │     ├─ 按 1 → 调用 vmx-voicemail-case 创建工单 → 播报 6 位工单号
  │     │         → 留言提示与提示音 → 开启 IVR 录音（仅客户语音）→ 客户按 # 结束
  │     │         → 停止录音 → "Your voicemail has been saved…" → 挂断
  │     └─ 其他按键或无输入 → 继续在队列中等候，客户队列流重新执行（再次回铃并提供留言选项）
  └─ 工单创建失败时仍继续录制留言（不播报工单号）
```

## 座席耳语联系流 `agent-whisper-case-notice.flow.json`

座席接起呼叫、与客户通话前播报："This caller has an open case, number <工单号，逐位播读>, owned by <负责人用户名>."

## 语言属性与转写

| 按键 | `language` | `vmx3_lang` / Transcribe 语言代码 |
|---|---|---|
| 1 | mandarin | zh-CN |
| 2 | cantonese | zh-HK |
| 3、超时或其他按键 | english | en-US |
| 4 | spanish | es-US |

语言仅作为联系人属性，用于确定转写语种与座席端标注，不参与路由。
