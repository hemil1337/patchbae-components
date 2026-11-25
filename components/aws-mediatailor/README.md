# AWS Elemental MediaTailor

Monetises and schedules streams with AWS Elemental MediaTailor. Set up
server-side ad insertion for a stream and get the URLs players use, or put a
VOD source on the schedule of a channel-assembly channel. Use it at the end
of a publishing workflow, once the stream or the file is ready.

## What it does

| Action | What happens |
| --- | --- |
| `configure_ad_insertion` | Saves a playback configuration that joins a stream's origin with an ad decision server, and returns the playback URL prefixes. |
| `schedule_program` | Creates a program on a channel-assembly channel that plays a VOD source, at a set time or next to another program, with optional ad breaks. |

No media passes through Patchbae.

`configure_ad_insertion` saves the whole playback configuration. Settings
this component has no input for, such as a CDN configuration, a bumper or
avail suppression, are reset when the configuration is saved. Use it for
configurations the workflow owns, not for ones you tune by hand.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `action` | yes | | `configure_ad_insertion` or `schedule_program`. |
| `configuration_name` | with `configure_ad_insertion` | | Name of the playback configuration. |
| `content_source_url` | with `configure_ad_insertion` | | URL prefix of the stream's origin, without the manifest's file name. |
| `ad_decision_server_url` | with `configure_ad_insertion` | | URL of the ad decision server, with any player variables. |
| `slate_ad_url` | no | | MP4 shown when a break is not filled. |
| `manifest_path` | no | | Path of the HLS manifest below the origin, for example `index.m3u8`. |
| `channel_name` | with `schedule_program` | | The channel-assembly channel. |
| `source_location_name` | with `schedule_program` | | The source location that holds the VOD source. |
| `vod_source_name` | with `schedule_program` | | The VOD source to play. |
| `program_name` | no | `patchbae-<task id>` | Name for the program. |
| `start_time` | no | | A UTC time such as `2026-11-25T19:30:00Z`. For channels in linear mode. |
| `relative_position` | no | `AFTER_PROGRAM` | Without a start time: `AFTER_PROGRAM` or `BEFORE_PROGRAM`. |
| `relative_program` | no | | Without a start time: the program to place this one next to. Empty means the end of the schedule. |
| `ad_break_offsets` | no | | Seconds from the start of the program at which ad breaks go, for example `[0, 600]`. |

Do not put a password or an API key in `ad_decision_server_url`. If your ad
server needs one, keep it in the ad server's own allow list or in a
MediaTailor configuration alias.

## Outputs

Outputs an action does not produce are empty.

| Output | From | Description |
| --- | --- | --- |
| `playback_configuration_arn` | `configure_ad_insertion` | The configuration's ARN. |
| `hls_playback_prefix` | `configure_ad_insertion` | The prefix HLS players use. Add the manifest's path. |
| `dash_playback_prefix` | `configure_ad_insertion` | The prefix DASH players use. |
| `session_initialization_prefix` | `configure_ad_insertion` | The prefix for starting sessions with client-side reporting. |
| `playback_url` | `configure_ad_insertion` | `hls_playback_prefix` plus `manifest_path`, when the path is set. |
| `program_name` | `schedule_program` | The program's name. |
| `program_arn` | `schedule_program` | The program's ARN. |
| `scheduled_start_time` | `schedule_program` | When the program starts, when MediaTailor reports it. |

With `manifest_path` set, the node adds the playback URL to the run's assets
as `Playback with ads`.

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the MediaTailor resources:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → AWS Elemental MediaTailor → Set up access**.

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
   `<REGION>`, `<ACCOUNT_ID>`, `<CHANNEL_NAME>` and `<SOURCE_LOCATION_NAME>`
   with your own. The role can then read and save playback configurations,
   and read and create programs on that one channel from that one source
   location. It cannot delete anything, and it cannot start or stop a
   channel. If you use one action only, remove the statement of the other.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

MediaTailor fetches the stream and the ads over HTTP itself. The component
passes it no role and reads nothing from S3.

## How it runs

- **Saves only what changed.** `configure_ad_insertion` reads the
  configuration first. When the origin, the ad server and the slate are
  already as asked, nothing is saved.
- **One program per name.** `schedule_program` looks for a program with the
  name first and returns it when there is one. A task that is delivered twice
  schedules one program.
- **Time budget.** 5 minutes. Both actions are a few API calls and no
  waiting.
- **A channel has to exist and a VOD source has to be registered** before a
  program can use them. This component creates neither.

## Cost

MediaTailor bills ad insertion by the ad impressions it serves and channel
assembly by the hours a channel runs. Saving a configuration and creating a
program cost nothing. Patchbae adds no charge. See the AWS Elemental
MediaTailor pricing page.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "MediaTailor would not save the playback configuration: …" | MediaTailor's reason follows, for example an origin URL it cannot use. |
| "MediaTailor would not schedule the program: …" | MediaTailor's reason follows. Common causes: the channel, source location or VOD source does not exist, a start time on a channel that is not in linear mode, or a relative program that is not on the schedule. |
| "Set start_time or relative_program, not both" | A program starts at a set time or next to another program. |
| Players get the stream without ads | The ad decision server returned nothing, or the manifest has no ad markers. Check the playback configuration's logs in CloudWatch. |
| `AccessDenied` on a `mediatailor:` action | The component's role is missing actions from `iam/policy.json`, or the placeholders were not replaced. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
