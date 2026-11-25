# AWS Elemental MediaLive

Controls a live channel in AWS Elemental MediaLive from a workflow: put it on
air before an event, take it off air afterwards, cue an ad break, or cut to
another input. Use it where a schedule, a message or a person's decision
should move a live channel.

## What it does

| Action | What happens |
| --- | --- |
| `start_channel` | Starts the channel and, by default, waits until it is `RUNNING`. |
| `stop_channel` | Stops the channel and, by default, waits until it is `IDLE`. |
| `insert_ad_break` | Adds a SCTE-35 splice insert to the channel's schedule: an ad break, now or at a set time. |
| `switch_input` | Adds an input switch to the channel's schedule: the channel cuts to another of its input attachments, now or at a set time. |

No media passes through Patchbae. The node only tells MediaLive what to do.

A channel costs money for every hour it runs. `stop_channel` at the end of an
event is what stops the charges.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `channel_id` | yes | | The channel's ID: the number at the end of its ARN. |
| `action` | yes | | One of `start_channel`, `stop_channel`, `insert_ad_break`, `switch_input`. |
| `wait` | no | `true` | For `start_channel` and `stop_channel`: wait until the channel is running or idle. |
| `ad_break_seconds` | no | | For `insert_ad_break`: length of the break in seconds. Empty means the break lasts until a return to network. |
| `input_attachment` | with `switch_input` | | Name of the input attachment to switch to, as shown on the channel. |
| `start_time` | no | | For `insert_ad_break` and `switch_input`: a UTC time such as `2026-11-25T19:30:00Z`. Empty means immediately. |

## Outputs

| Output | Description |
| --- | --- |
| `channel_id` | The channel the action was applied to. |
| `channel_arn` | The channel's ARN. |
| `channel_name` | The channel's name. |
| `state` | The channel's state when the node finished, for example `RUNNING` or `IDLE`. |
| `action_name` | The schedule action that was added (`insert_ad_break`, `switch_input`). Empty otherwise. |
| `action_time` | When the schedule action takes effect: a UTC time, or `immediate`. |

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the channel:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → AWS Elemental MediaLive → Set up access**.

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Principal": {
           "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/<OIDC_PROVIDER>"
         },
         "Action": "sts:AssumeRoleWithWebIdentity",
         "Condition": {
           "StringEquals": {
             "<OIDC_PROVIDER>:aud": "sts.amazonaws.com",
             "<OIDC_PROVIDER>:sub": "system:serviceaccount:<NAMESPACE>:<SERVICE_ACCOUNT>"
           }
         }
       }
     ]
   }
   ```

2. **Attach the permissions in [`iam/policy.json`](iam/policy.json).** Replace
   `<REGION>`, `<ACCOUNT_ID>` and `<CHANNEL_ID>` with your own. The role can
   then read the channel and its schedule, start and stop it, and add
   schedule actions to it, and nothing else. For more than one channel, list
   one resource per channel.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

The component passes no role to MediaLive and reads nothing from S3. The
channel keeps using the role it was created with.

## How it runs

- **Looks before it acts.** `start_channel` on a channel that is already
  running does nothing, and so does `stop_channel` on an idle one. A task
  that is delivered twice therefore starts or stops the channel once.
- **One schedule action per task.** The action is named `patchbae-<task id>`.
  A second delivery finds it in the schedule and adds nothing. The splice
  event id of an ad break is also worked out from the task id.
- **Waits politely.** While a channel starts or stops, the node asks for its
  state every 5 seconds at first and every 30 seconds later.
- **Time budget.** 30 minutes. A channel that is still starting when the node
  is stopped keeps starting in AWS.
- **Immediate actions need a running channel.** MediaLive only accepts an
  immediate ad break or input switch while the channel runs. With
  `start_time` set, the action can be added while the channel is idle.

## Cost

MediaLive bills a running channel by the hour, by input and output
resolution, codec and bitrate. Schedule actions cost nothing. Patchbae adds
no charge. See the AWS Elemental MediaLive pricing page for your region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "Channel … was not found in this region" | The ID is wrong or the component is set up for another region. Channels are per region. |
| "MediaLive refused the schedule action: …" | MediaLive's own reason follows. Common causes: the channel is not running (immediate actions), the time is in the past or too close, or the channel's outputs do not carry SCTE-35. |
| "has no input attachment named …" | The message lists the channel's input attachments. Use one of those names. |
| "Channel … is UPDATE_FAILED and cannot be started" | Fix the channel in the MediaLive console first. |
| `AccessDenied` on `DescribeChannel`, `StartChannel`, `StopChannel`, `DescribeSchedule` or `BatchUpdateSchedule` | The component's role is missing actions from `iam/policy.json`, or `<REGION>`, `<ACCOUNT_ID>` and `<CHANNEL_ID>` were not replaced. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
