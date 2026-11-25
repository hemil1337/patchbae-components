# Amazon IVS

Works with Amazon Interactive Video Service (IVS), the managed live streaming
service with a few seconds of latency. Use it to set up a channel for an
event, check whether its stream is live and healthy, end a stream, or send
viewers a cue that arrives in step with the video.

## What it does

| Action | What happens |
| --- | --- |
| `create_channel` | Creates a channel and returns where to send the stream and where to watch it. A channel with the same name is reused. |
| `get_stream` | Returns whether the channel is live, the stream's health and the number of viewers. |
| `stop_stream` | Ends the channel's live stream. The encoder is disconnected. |
| `put_metadata` | Sends up to 1 KB of text to viewers, in step with the video (timed metadata). |

No media passes through Patchbae.

The stream key that an encoder needs is a secret. `create_channel` returns
the key's ARN, never its value. Read the value in the IVS console, or with
`GetStreamKey` from the system that configures the encoder.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `action` | yes | | One of `create_channel`, `get_stream`, `stop_stream`, `put_metadata`. |
| `channel_name` | with `create_channel` | | Letters, digits, dashes and underscores, at most 128 characters. |
| `channel_arn` | with the other actions | | ARN of the channel. |
| `latency_mode` | no | `LOW` | For `create_channel`: `LOW` or `NORMAL`. |
| `channel_type` | no | `STANDARD` | For `create_channel`: `STANDARD`, `BASIC`, `ADVANCED_SD` or `ADVANCED_HD`. |
| `recording_configuration_arn` | no | | For `create_channel`: record every stream to S3 with this recording configuration. |
| `metadata` | with `put_metadata` | | The text to send, at most 1 KB. Often JSON. |

## Outputs

Outputs an action does not produce are empty (`viewer_count` is `0`).

| Output | From | Description |
| --- | --- | --- |
| `channel_arn` | all | The channel's ARN. |
| `channel_name` | `create_channel` | The channel's name. |
| `ingest_server` | `create_channel` | The RTMPS address an encoder sends to, for example `rtmps://a1b2c3d4e5f6.global-contribute.live-video.net:443/app/`. |
| `playback_url` | `create_channel`, `get_stream` | The HLS address viewers play. |
| `stream_key_arn` | `create_channel` | ARN of the stream key. Not the key. |
| `state` | `get_stream`, `stop_stream`, `put_metadata` | `LIVE` or `OFFLINE`. |
| `health` | `get_stream` | `HEALTHY`, `STARVING` or `UNKNOWN`. |
| `viewer_count` | `get_stream` | How many viewers are watching. |
| `stream_id` | `get_stream` | The live stream's ID. |

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the IVS channels:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → Amazon IVS → Set up access**.

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
   `<REGION>` and `<ACCOUNT_ID>` with your own. The role can then create
   channels and tag them, find and read channels, and read, stop and annotate
   their streams. It cannot delete channels and it cannot read a stream key's
   value (`ivs:GetStreamKey` is not in the policy). To limit the role to
   channels you already have, replace `channel/*` in the last statement with
   their ARNs and remove the first statement.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

## How it runs

- **One channel per name.** `create_channel` looks for a channel with the
  same name first and returns it when there is one. A task that is delivered
  twice creates one channel. So does a second run with the same name: use a
  name per event.
- **`stop_stream` on an offline channel succeeds.** The stream is already
  ended, which is what was asked for.
- **`put_metadata` can arrive twice.** IVS has no way to ask whether a piece
  of metadata was already sent, so a task that is delivered twice sends it
  twice. Put an id in the metadata and have the player ignore one it has
  seen.
- **`put_metadata` needs a live stream** and fails with a clear message when
  the channel is offline.
- **Time budget.** 5 minutes. Every action is a few API calls and no waiting.

## Cost

IVS bills live video input by the hour, by channel type, and video output by
the hours viewers watch. A channel without a stream costs nothing. Patchbae
adds no charge. See the Amazon IVS pricing page.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "The channel was not found" | The ARN is wrong or the component is set up for another region. |
| "The channel is not live, so there is no stream to add metadata to" | Send metadata only while the encoder is streaming. Check with `get_stream` first. |
| "IVS would not create the channel: …" | IVS's reason follows, for example a quota on channels or a recording configuration in another region. |
| "metadata is … bytes" | Shorten the text. IVS accepts 1 KB per request. |
| `ThrottlingException` on `PutMetadata` | IVS limits how often metadata can be sent to one channel. Space the nodes out. |
| `AccessDenied` on an `ivs:` action | The component's role is missing actions from `iam/policy.json`, or `<REGION>` and `<ACCOUNT_ID>` were not replaced. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
