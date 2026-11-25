# AWS Elemental Inference

Sets up and steers AWS Elemental Inference, the service that runs AI on a
live stream while it is on air: vertical crops for social platforms, clips of
key moments, subtitles and descriptive metadata. Use it before a live event
to prepare a feed, and during the event to switch features on and off.

## What it does

| Action | What happens |
| --- | --- |
| `create_feed` | Creates a feed with one output per feature you list, and returns the feed's ARN and data endpoints. A feed with the same name is reused. |
| `set_output_status` | Switches one output of a feed on or off, now or at a set time. |
| `find_fixture` | Looks up a sports fixture by sport, date and team, and returns its ID for a clips output. |

A feed does nothing until a stream reaches it. Attach the feed to the
MediaLive channel that carries the event (in MediaLive, under the channel's
Elemental Inference settings, with `feed_arn`). The channel then sends the
stream to the feed. No media passes through Patchbae.

| Feature | Output name | What the feed produces |
| --- | --- | --- |
| `smart_crop` | `crop` | A vertical crop that follows the action, with optional graphics templates. |
| `clips` | `clips` | Metadata that marks key moments for clipping. With `fixture_id`, the moments follow that fixture's event data. |
| `subtitles` | `subtitles` | Live subtitles in the stream's language. |
| `contextual_metadata` | `metadata` | Descriptions of what is on screen, shot by shot. |

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `action` | yes | | `create_feed`, `set_output_status` or `find_fixture`. |
| `feed_name` | with `create_feed` | | Letters, digits, dashes and underscores. |
| `features` | with `create_feed` | | Any of `smart_crop`, `clips`, `subtitles`, `contextual_metadata`. |
| `subtitle_language` | no | `eng` | With `subtitles`: `eng`, `eng-au`, `eng-gb`, `eng-us`, `fra`, `ita`, `deu`, `spa`, `por` or `ara`. |
| `fixture_id` | no | | With `clips`: the fixture to follow. |
| `crop_template_uris` | no | | With `smart_crop`: S3 URIs of graphics templates. Needs `access_role_arn`. |
| `access_role_arn` | with templates | | ARN of the role Elemental Inference reads the templates with. An ARN, not a credential. |
| `feed_id` | with `set_output_status` | | The feed's ID. |
| `output_name` | with `set_output_status` | | The output to switch, for example `clips`. |
| `enabled` | no | `true` | `true` switches the output on, `false` off. |
| `at` | no | | A UTC time such as `2026-11-25T19:00:00Z`. Empty means now. |
| `sport` | with `find_fixture` | | `basketball` or `american-football`. |
| `date` | with `find_fixture` | | The day of the fixture in UTC, `YYYY-MM-DD`. |
| `end_date` | no | | Last day of the search. Empty searches one day. |
| `competitors` | no | | Team names. A fixture matches when one of them plays. |

## Outputs

Outputs an action does not produce are empty.

| Output | From | Description |
| --- | --- | --- |
| `feed_id` | `create_feed`, `set_output_status` | The feed's ID. |
| `feed_arn` | `create_feed` | The feed's ARN, for the MediaLive channel. |
| `feed_status` | `create_feed` | `AVAILABLE` until a channel is associated, `ACTIVE` after. |
| `data_endpoints` | `create_feed` | The feed's data endpoints. |
| `output_names` | `create_feed` | The names of the feed's outputs. |
| `output_name` | `set_output_status` | The output that was switched. |
| `output_status` | `set_output_status` | `ENABLED` or `DISABLED`. |
| `action_name` | `set_output_status` with `at` | The schedule action, `patchbae-<task id>`. |
| `scheduled_at` | `set_output_status` | A UTC time, or `now`. |
| `fixture_id` | `find_fixture` | The earliest fixture that matched. |
| `fixture_name` | `find_fixture` | That fixture's name. |
| `fixture_count` | `find_fixture` | How many fixtures matched. |
| `fixtures` | `find_fixture` | Every match, earliest first. |

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the feeds:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → AWS Elemental Inference → Set up access**.

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
   `<REGION>`, `<ACCOUNT_ID>` and `<ACCESS_ROLE_NAME>` with your own. The
   role can then create, find, read and change feeds, add schedule actions,
   search fixtures and hand the access role to Elemental Inference. It cannot
   delete a feed. If you use no crop templates, remove the last statement.

   Creating a feed, listing feeds and searching fixtures have no resource to
   limit them to, so those statements use `"Resource": "*"`. The ARN format
   of a feed is taken from the service's naming and was not checked against a
   live account: compare it with the feed ARN the node returns, and with
   "Actions, resources, and condition keys for AWS Elemental Inference" in
   the Service Authorization Reference.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

### The access role (only for crop templates)

Elemental Inference reads graphics templates from S3 through a role of its
own. Create a role that `elemental-inference.amazonaws.com` can assume, give
it `s3:GetObject` on the templates, put its ARN in `access_role_arn` and its
name in place of `<ACCESS_ROLE_NAME>`.

## How it runs

- **One feed per name.** `create_feed` looks for a feed with the name first
  and returns it when there is one. A task that is delivered twice creates
  one feed. So does a second run with the same name: use a name per event.
  A feed that is found is returned as it is; its outputs are not changed to
  match `features`.
- **Switching now changes only that output.** The node reads the feed, and
  sends it back with one status changed. An output that already has the
  status is left alone.
- **One schedule action per task.** With `at` set, the action is named
  `patchbae-<task id>`. A second delivery finds it and adds nothing.
- **Scheduled times are approximate.** Elemental Inference applies a change
  within a few minutes of the time.
- **Time budget.** 15 minutes, for a feed that is slow to become available.
- **SDK version.** This component needs a newer AWS SDK than the rest of the
  library (`boto3==1.44.9`), so it gets an image of its own at install.

## Cost

Elemental Inference bills by the minute of stream each output processes. A
feed with no stream, or with its outputs switched off, is not billed for
processing. Patchbae adds no charge. See the AWS Elemental Inference pricing
page.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "Elemental Inference would not create the feed: …" | The service's reason follows, for example a quota on feeds or a template it cannot read. |
| "has no output named …" | The message lists the feed's outputs. Use one of those names. |
| "Elemental Inference would not change the feed: …" | The feed is being updated by something else, or the change is not allowed in its state. Try again shortly. |
| "No basketball fixture … was found" | The date is in UTC: an evening game in the Americas is on the next day. Check the team names against the league's own. |
| The feed stays `AVAILABLE` | No channel is associated with it yet. Attach the feed to the MediaLive channel. |
| `AccessDenied` on an `elemental-inference:` action | The component's role is missing actions from `iam/policy.json`, or the feed ARN format in the policy does not match. See step 2. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
