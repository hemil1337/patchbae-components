# AWS Elemental MediaConvert

Transcodes a media file on S3 with AWS Elemental MediaConvert: to an MP4 for
review or delivery, to an HLS ladder for streaming, or to whatever one of your
own job templates produces. Use it after ingest to make a proxy, or before
delivery to make the files a platform asks for.

## What it does

1. Submits a MediaConvert job for the file at `media_uri`.
2. Waits for the job to finish.
3. Returns the job id and where the outputs were written.

The media file never passes through Patchbae. MediaConvert reads it from S3
and writes its outputs to S3; the node only holds references.

| Preset | What it writes |
| --- | --- |
| `mp4_h264_1080p` | One MP4: H.264 at 1920×1080 (QVBR quality 8, at most 8 Mbit/s) with stereo AAC at 128 kbit/s. |
| `mp4_h264_720p` | One MP4: H.264 at 1280×720 (QVBR quality 7, at most 4 Mbit/s) with stereo AAC at 128 kbit/s. |
| `hls_abr` | HLS with three renditions in 6-second segments: 1080p (at most 6 Mbit/s), 720p (3.5 Mbit/s) and 360p (1.2 Mbit/s). |
| `job_template` | Whatever your job template defines. Only the input file and the output destination are replaced. |

The built-in presets keep the frame rate of the source, use its default audio
track, and fit other aspect ratios inside the frame with black bars. For
anything else (captions, more audio tracks, other codecs) make a job template
in MediaConvert and use `job_template`.

Outputs are named after the input file. `s3://media/in/show.mxf` with
`mp4_h264_1080p` and `output_uri` `s3://work/transcodes/` becomes
`s3://work/transcodes/show_1080p.mp4`. With `hls_abr` it becomes the master
playlist `s3://work/transcodes/show.m3u8`, with the rendition playlists and
segments next to it.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `media_uri` | yes | | S3 URI of the file to transcode. |
| `output_uri` | yes | | S3 folder for the outputs. Must end with a slash, for example `s3://acme-media-work/transcodes/`. |
| `mediaconvert_role_arn` | yes | | ARN of the IAM role MediaConvert assumes to read and write S3. See **AWS access**. It is an ARN, not a credential. |
| `preset` | no | `mp4_h264_1080p` | One of `mp4_h264_1080p`, `mp4_h264_720p`, `hls_abr`, `job_template`. |
| `job_template` | with `job_template` | | Name of a MediaConvert job template in the same region. |
| `queue` | no | | Name or ARN of the queue to submit to. Empty means the default queue, or the template's queue. |
| `name_modifier` | no | | Text added to the input's file name. Empty means `_1080p` or `_720p` for the MP4 presets and nothing for `hls_abr` and `job_template`. |

## Outputs

| Output | Description |
| --- | --- |
| `job_id` | The MediaConvert job id, to find it in the AWS console. |
| `output_uri` | The folder the outputs were written to. |
| `output_uris` | The files written. For HLS, DASH and CMAF this is the manifest, not every segment. |
| `status` | The job's final status. `COMPLETE` when the node succeeds. |

`output_uris` is worked out from the job settings. With a job template that
uses format identifiers such as `$dt$` in its names, the real names differ.

The node also adds the first file output to the run's assets as `Transcode`
and an HLS master playlist as `HLS`, so a later node can use
`{{assets.Transcode.locator}}` or `{{assets.HLS.locator}}`.

## AWS access

Two IAM roles are involved, and they do different things.

| Role | Who assumes it | What it may do |
| --- | --- | --- |
| The component's role | This component's pod | Submit and follow MediaConvert jobs, and hand the service role to MediaConvert. No S3 access. |
| The MediaConvert service role | MediaConvert itself | Read the media and write the outputs on S3. |

### The component's role

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the media:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → AWS Elemental MediaConvert → Set up access**.

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
   `<REGION>`, `<ACCOUNT_ID>` and `<MEDIACONVERT_ROLE_NAME>` (the name of the
   service role below) with your own. The role can then submit jobs, read
   their status, find the job of an earlier delivery, read job templates and
   pass the service role to MediaConvert, and nothing else. It has no access
   to S3: MediaConvert reads and writes the files through the service role.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

### The MediaConvert service role

MediaConvert does the reading and writing, so it needs a role of its own. If
you already run MediaConvert jobs you have one; otherwise create it:

1. **Create an IAM role that MediaConvert can assume**, with this trust
   policy:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Principal": { "Service": "mediaconvert.amazonaws.com" },
         "Action": "sts:AssumeRole"
       }
     ]
   }
   ```

2. **Give it read access to the media and write access to the outputs.**
   Replace `<MEDIA_BUCKET>`, `<OUTPUT_BUCKET>` and `<OUTPUT_PREFIX>`:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "ReadMedia",
         "Effect": "Allow",
         "Action": ["s3:GetObject"],
         "Resource": "arn:aws:s3:::<MEDIA_BUCKET>/*"
       },
       {
         "Sid": "WriteOutputs",
         "Effect": "Allow",
         "Action": ["s3:PutObject"],
         "Resource": "arn:aws:s3:::<OUTPUT_BUCKET>/<OUTPUT_PREFIX>*"
       },
       {
         "Sid": "LocateBuckets",
         "Effect": "Allow",
         "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
         "Resource": [
           "arn:aws:s3:::<MEDIA_BUCKET>",
           "arn:aws:s3:::<OUTPUT_BUCKET>"
         ]
       }
     ]
   }
   ```

3. **Put this role's ARN in the node's `mediaconvert_role_arn` input**, and
   its name in place of `<MEDIACONVERT_ROLE_NAME>` in the component's policy.
   The component's role may pass this one role to MediaConvert and no other.

Buckets encrypted with your own KMS key also need `kms:Decrypt` and
`kms:GenerateDataKey` on that key in the service role.

## How it runs

- **One job per task.** Every job carries the task id. If the task is
  delivered twice, the second delivery finds the first job and waits for it
  instead of starting (and paying for) another one. A job that failed on an
  earlier attempt is left alone and a new one is started.
- **Time budget.** Up to 6 hours. If the node is stopped, the MediaConvert
  job keeps running in AWS and a retry picks it up.
- **Small pod.** The pod only makes API calls, so it needs little CPU and
  memory regardless of file size.
- **Job templates.** The component reads the template, points every output
  group at `output_uri` and submits the job with the template's name. The
  template's other settings, including its queue, apply unchanged.

## Cost

You pay MediaConvert per minute of output, by resolution, codec and quality
setting, plus S3 storage for the outputs. `hls_abr` writes three outputs and
is billed for each. Patchbae adds no charge. See the AWS Elemental
MediaConvert pricing page for your region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `AccessDenied` on `CreateJob`, `GetJob`, `SearchJobs` or `GetJobTemplate` | The component's role is missing actions from `iam/policy.json`, or `<REGION>` and `<ACCOUNT_ID>` were not replaced. |
| "is not authorized to perform: iam:PassRole" | The role in `mediaconvert_role_arn` is not the one named in the component's policy. Fix `<MEDIACONVERT_ROLE_NAME>`. |
| The node fails with "MediaConvert could not transcode the file" and an S3 access error | The MediaConvert service role cannot read `media_uri` or write to `output_uri`. Check its policy and the bucket's KMS key. |
| "MediaConvert rejected the job: …" | MediaConvert refused the settings. With a job template, open the template in the console and check it runs there. |
| "Job template … was not found in this region" | The template lives in another region or has another name. Templates are per region. |
| "output_uri must be a folder" | End `output_uri` with `/`. |
| "The output would overwrite media_uri" | The output has the same name and folder as the input. Change `output_uri` or set `name_modifier`. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
