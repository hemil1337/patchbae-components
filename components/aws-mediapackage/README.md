# AWS Elemental MediaPackage

Turns live into on-demand with AWS Elemental MediaPackage. Harvest a time
range from a live channel to S3 as soon as an event ends, or publish a file
that is already on S3 as a VOD asset and get its playback URLs. Use both in
one workflow for catch-up TV: harvest, then publish.

## What it does

| Action | Service | What happens |
| --- | --- | --- |
| `harvest_clip` | MediaPackage v2 | Starts a harvest job that copies a time range of a live origin endpoint to an S3 folder, and waits for it. |
| `publish_vod_asset` | MediaPackage VOD | Creates a VOD asset from an HLS playlist, SMIL manifest or MP4 on S3, waits until it is playable and returns one playback URL per packaging configuration. |

The media never passes through Patchbae. MediaPackage writes the harvested
clip to S3 and reads the VOD source from S3 itself.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `action` | yes | | `harvest_clip` or `publish_vod_asset`. |
| `channel_group` | with `harvest_clip` | | Name of the channel group. |
| `channel` | with `harvest_clip` | | Name of the channel. |
| `origin_endpoint` | with `harvest_clip` | | Name of the origin endpoint to harvest from. |
| `hls_manifests` | one manifest list with `harvest_clip` | | Names of the HLS manifests to harvest. |
| `dash_manifests` | | | Names of the DASH manifests to harvest. |
| `low_latency_hls_manifests` | | | Names of the low-latency HLS manifests to harvest. |
| `start_time` | with `harvest_clip` | | Start of the clip, a UTC time such as `2026-11-25T19:00:00Z`. |
| `end_time` | with `harvest_clip` | | End of the clip. After `start_time`, and in the past. |
| `output_uri` | with `harvest_clip` | | S3 folder for the clip. A folder inside the bucket, ending with a slash. |
| `source_uri` | with `publish_vod_asset` | | S3 URI of an `.m3u8`, `.smil` or `.mp4` file. |
| `packaging_group_id` | with `publish_vod_asset` | | ID of the VOD packaging group. |
| `source_role_arn` | with `publish_vod_asset` | | ARN of the role MediaPackage assumes to read the source. See **AWS access**. It is an ARN, not a credential. |
| `asset_id` | no | `patchbae-<task id>` | ID for the VOD asset. |

The time range of a harvest has to lie inside the origin endpoint's startover
window, so run `harvest_clip` soon after the event ends.

## Outputs

Outputs an action does not produce are empty.

| Output | From | Description |
| --- | --- | --- |
| `job_name` | `harvest_clip` | The harvest job's name, `patchbae-<task id>`. |
| `job_arn` | `harvest_clip` | The harvest job's ARN. |
| `status` | both | `COMPLETED` for a harvest, `PLAYABLE` for an asset. |
| `output_uri` | `harvest_clip` | The folder the clip was written to. |
| `asset_id` | `publish_vod_asset` | The asset's ID. |
| `asset_arn` | `publish_vod_asset` | The asset's ARN. |
| `playback_urls` | `publish_vod_asset` | A list of `{"packaging_configuration_id", "url", "status"}`. |
| `playback_url` | `publish_vod_asset` | The first playback URL. |

`harvest_clip` adds the output folder to the run's assets as `Harvest`.
`publish_vod_asset` adds each playback URL as `Playback <packaging
configuration id>`.

## AWS access

`harvest_clip` and `publish_vod_asset` reach S3 in different ways, and
neither goes through the component's role.

| Who | Does what | How it is allowed |
| --- | --- | --- |
| The component's role | Starts and follows harvest jobs, creates and reads VOD assets, hands the VOD role to MediaPackage. No S3 access. | `iam/policy.json` |
| MediaPackage v2 (harvest) | Reads the origin endpoint and writes the clip to S3. | A policy on the origin endpoint and a policy on the bucket. No role. |
| MediaPackage VOD (publish) | Reads the source file from S3. | A role it assumes, named in `source_role_arn`. |

### The component's role

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the MediaPackage resources:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → AWS Elemental MediaPackage → Set up access**.

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
   `<REGION>`, `<ACCOUNT_ID>`, `<CHANNEL_GROUP>`, `<PACKAGING_GROUP_ID>` and
   `<MEDIAPACKAGE_ROLE_NAME>` (the name of the VOD role below) with your own.
   If you use one action only, remove the statements of the other.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

### Harvest: the endpoint policy and the bucket policy

A harvest job runs as the MediaPackage service itself. It needs two grants,
both to the service principal `mediapackagev2.amazonaws.com`:

1. **On the origin endpoint:** an endpoint policy that allows
   `mediapackagev2:HarvestObject`.
2. **On the destination bucket:** a bucket policy that allows
   `s3:PutObject` on the folder the clips go to. Replace `<OUTPUT_BUCKET>`,
   `<OUTPUT_PREFIX>` and `<ACCOUNT_ID>`:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "AllowMediaPackageHarvestToWrite",
         "Effect": "Allow",
         "Principal": { "Service": "mediapackagev2.amazonaws.com" },
         "Action": "s3:PutObject",
         "Resource": "arn:aws:s3:::<OUTPUT_BUCKET>/<OUTPUT_PREFIX>*",
         "Condition": {
           "StringEquals": { "aws:SourceAccount": "<ACCOUNT_ID>" }
         }
       }
     ]
   }
   ```

The MediaPackage user guide has the endpoint policy in full, under "Origin
endpoint authorization" and "Creating live-to-VOD assets". Follow it if it
differs from the above.

### Publish: the MediaPackage VOD role

1. **Create an IAM role that MediaPackage can assume**, with this trust
   policy:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Principal": { "Service": "mediapackage.amazonaws.com" },
         "Action": "sts:AssumeRole"
       }
     ]
   }
   ```

2. **Give it read access to the source bucket.** Replace `<MEDIA_BUCKET>`:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "ReadVodSources",
         "Effect": "Allow",
         "Action": ["s3:GetObject", "s3:GetBucketLocation", "s3:GetBucketRequestPayment", "s3:ListBucket"],
         "Resource": ["arn:aws:s3:::<MEDIA_BUCKET>", "arn:aws:s3:::<MEDIA_BUCKET>/*"]
       }
     ]
   }
   ```

3. **Put this role's ARN in the node's `source_role_arn` input**, and its
   name in place of `<MEDIAPACKAGE_ROLE_NAME>` in the component's policy.

## How it runs

- **One harvest job per task.** The job is named `patchbae-<task id>`. A
  second delivery finds it and waits for it instead of harvesting again. A
  job that failed stays failed: fix the cause and start a new run.
- **One asset per ID.** `publish_vod_asset` looks for an asset with the ID
  first and returns it when there is one.
- **Waits politely.** The node asks for the job's or the asset's status every
  5 seconds at first and every 30 seconds later.
- **Time budget.** 2 hours. If the node is stopped, the harvest job keeps
  running in AWS and a retry picks it up.
- **The bucket of a VOD source must be in the same region** as the packaging
  group.

## Cost

MediaPackage bills a harvest by the gigabyte harvested, and VOD by the
gigabyte ingested and packaged, plus S3 storage for the clip. Patchbae adds
no charge. See the AWS Elemental MediaPackage pricing page for your region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "MediaPackage would not start the harvest: …" | MediaPackage's reason follows. Common causes: the time range is outside the startover window, a manifest name is wrong, or the endpoint or bucket policy is missing. |
| "MediaPackage could not harvest the clip: …" | The job started and failed. The reason is usually the bucket policy or a KMS key on the bucket. |
| "MediaPackage would not create the asset: …" | The packaging group does not exist in this region, or the source ARN is not readable. |
| "MediaPackage could not process asset …" | The VOD role cannot read `source_uri`, or the source is in a format the packaging group does not accept. |
| "is not authorized to perform: iam:PassRole" | The role in `source_role_arn` is not the one named in the component's policy. Fix `<MEDIAPACKAGE_ROLE_NAME>`. |
| `AccessDenied` on a `mediapackagev2:` or `mediapackage-vod:` action | The component's role is missing actions from `iam/policy.json`, or the placeholders were not replaced. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
