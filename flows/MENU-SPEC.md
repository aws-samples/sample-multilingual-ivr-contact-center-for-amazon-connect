# IVR menu specification

**🌐 Language / 语言:** **English** · [简体中文](MENU-SPEC.zh-CN.md)

This document describes the call logic and key presses of the three contact flow templates. The `{{PLACEHOLDER}}` values in the templates are replaced with actual resource IDs by CDK at deployment (see `_flow_content` in `cdk/stacks/connect_ivr_stack.py`). All prompts use the Amazon Polly voice Joanna (English).

## Inbound flow `ivr-main.flow.json`

```
Call arrives
  ├─ 1. Caller identification: GetCustomerProfile (_phone = caller number)
  │     ├─ Match    → AssociateContactToCustomerProfile
  │     └─ No match → CreateCustomerProfile → AssociateContactToCustomerProfile
  │     └─ Store the contact attribute vm_profile_id; if any step fails, skip identification and continue
  ├─ 2. Set the VMX flow attributes and the prompt voice Joanna
  ├─ 3. Enable call recording (agent and customer; no recording during the IVR)
  ├─ 4. Set the customer queue flow ivr-customer-queue
  ├─ 5. Recording notice: "Thank you for calling. This call may be recorded for quality assurance."
  ├─ 6. Language menu (DTMF, 6-second timeout)
  │     ├─ 1 → language=mandarin,  vmx3_lang=zh-CN
  │     ├─ 2 → language=cantonese, vmx3_lang=zh-HK
  │     ├─ 3 → language=english,   vmx3_lang=en-US
  │     ├─ 4 → language=spanish,   vmx3_lang=es-US
  │     └─ Timeout or any other key → same as 3 (English)
  ├─ 7. Set the target queue main (same for all branches) and store vmx3_queue_arn
  ├─ 8. Returning-caller routing: invoke case-owner-lookup (on failure, go straight to the queue)
  │     ├─ Open case exists     → set the agent whisper flow agent-whisper-case-notice
  │     └─ route_to_owner=true  → UpdateContactRoutingCriteria (owner only, expires after owner_wait_seconds)
  └─ 9. Transfer to queue main; if the transfer fails or the queue is full →
        "Sorry, we are unable to take your call right now…" → disconnect
```

## Customer queue flow `customer-queue.flow.json`

Based on the Voicemail Express (VMX) V3 `VMX3_In_Queue_Option` flow, with ringback added before the voicemail offer.

```
Contact enters the queue
  ├─ 1. "Please hold while we connect you to an agent. Please stay on the line."
  ├─ 2. Ringback prompt Ringback_CN_450 (about 30 seconds); if an agent becomes available,
  │     Amazon Connect interrupts playback and connects the call
  ├─ 3. Voicemail offer: "…To leave a voicemail, press 1, or press any other key to remain in queue."
  │     ├─ 1 → invoke vmx-voicemail-case to create the case → read out the 6-digit ticket number
  │     │      → voicemail greeting and tone → enable IVR recording (caller audio only)
  │     │      → caller presses # → stop recording → "Your voicemail has been saved…" → disconnect
  │     └─ Any other key or no input → stay in the queue; the customer queue flow runs again
  │        (ringback and voicemail offer repeat)
  └─ If case creation fails, the voicemail is still recorded (without a ticket number)
```

## Agent whisper flow `agent-whisper-case-notice.flow.json`

Played to the agent after they accept the call and before they talk to the caller: "This caller has an open case, number <case number, digit by digit>, owned by <owner user name>."

## Language attributes and transcription

| Key | `language` | `vmx3_lang` / Transcribe language code |
|---|---|---|
| 1 | mandarin | zh-CN |
| 2 | cantonese | zh-HK |
| 3, timeout or any other key | english | en-US |
| 4 | spanish | es-US |

The language is only a contact attribute: it selects the transcription language and labels the contact for the agent. It does not affect routing.
