# Amazon Rekognition Video

Looks at a video on S3 and reports what is in it: labels (objects, scenes,
activities), unsafe or sensitive content, and where each shot starts and ends.
Use it after ingest to make content searchable, to hold back material that
needs a compliance review, or to give an editor or an agent a shot list.

## What it does

1. Starts one Amazon Rekognition Video analysis for each entry in `analyses`.
2. Waits for all of them to finish.
3. Returns a summary: the labels and moderation labels it is most sure of,
   whether anything was flagged, and the shots.
4. If `output_uri` is set, writes the complete results there as one JSON file.

The video never passes through Patchbae. Rekognition reads it from S3; the
node only holds references and the summary.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `media_uri` | yes | | S3 URI of the video. MP4 or MOV with H.264 video, up to 10 GB and 6 hours. |
| `analyses` | no | `[labels, moderation]` | What to look for. Any of `labels`, `moderation`, `shots`. |
| `min_confidence` | no | `80` | Labels and moderation labels below this confidence (0 to 100) are left out. Shots are not filtered. |
| `output_uri` | no | | S3 folder for the complete results. Must end with a slash, for example `s3://acme-media-work/analysis/`. |
| `max_items` | no | `50` | How many labels and moderation labels the node output lists, 1 to 1000. |

## Outputs

| Output | Description |
| --- | --- |
| `labels` | One entry per label, most confident first: `{"name": "Car", "confidence": 98.2, "timestamp_ms": 4000}`. `confidence` is the highest seen and `timestamp_ms` the first time the label appears. Cut at `max_items`. |
| `moderation_labels` | Unsafe or sensitive content, in the same shape as `labels`. |
| `flagged` | `true` when any moderation label is at or above `min_confidence`. |
| `shot_count` | The number of shots. |
| `shots` | The shots in order: `{"start_ms": 0, "end_ms": 4120}`. At most the first 1,000; `shot_count` is always the full count. |
| `results_uri` | The JSON file with the complete results. Only when `output_uri` is set. |

An analysis that was not asked for leaves its outputs empty: `[]`, `false`
or `0`.

The results file is named `patchbae-<task id>-rekognition.json`. It holds
`media_uri`, `min_confidence` and every result Rekognition returned, under
Rekognition's own names: `Labels`, `ModerationLabels` and `Segments`. The
node adds it to the run's assets as `Video analysis`, so a later node can use
`{{assets.Video analysis.locator}}`.

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the media:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → Amazon Rekognition Video → Set up access**.

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
   `<MEDIA_BUCKET>`, `<OUTPUT_BUCKET>` and `<OUTPUT_PREFIX>` with your own.
   The role can then start and read the three kinds of analysis, read the
   media and write the results file, and nothing else. If you never set
   `output_uri`, leave the `WriteResults` statement out. The Rekognition
   actions have `*` as their resource because Rekognition does not support
   anything narrower for them.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

Rekognition reads the video with the component's role, so that role needs
`s3:GetObject` on the file. The media bucket must be in the region the
component runs in.

## How it runs

- **One job per analysis per task.** Each analysis is started with a token
  made from the task id. If the task is delivered twice, Rekognition returns
  the jobs it already started instead of starting (and billing) new ones.
  This also means an analysis that failed stays failed for that task: run the
  workflow again to analyse the file afresh.
- **Side by side.** All analyses are started before the node waits, so three
  analyses take about as long as the slowest one.
- **Busy accounts.** Rekognition runs a limited number of video analyses at
  once per account. When that limit is reached the node waits for a free
  slot instead of failing.
- **Time budget.** Up to 4 hours, including any wait for a free slot. If the
  node is stopped, the analyses keep running in AWS and a retry picks them up.
- **Memory.** The pod asks for 512 MiB, because the complete results of a
  long video are held in memory before they are written to S3.

## Cost

You pay Amazon Rekognition per minute of video, separately for each
analysis: a 10-minute video with `labels`, `moderation` and `shots` is billed
as 30 minutes. S3 storage for the results file is extra. Patchbae adds no
charge. See the Amazon Rekognition pricing page for your region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `AccessDeniedException` on a `Start…` or `Get…` call | The role is missing the `rekognition:*` actions in `iam/policy.json`. |
| "Rekognition cannot read media_uri" | The file does not exist, the role lacks `s3:GetObject` on it, or the bucket is in another region. |
| "Rekognition could not analyse the video (…): …" | Rekognition rejected the file, most often because of its codec or container. Transcode it to an H.264 MP4 first, for example with the AWS Elemental MediaConvert component. |
| "The video is too large for Rekognition Video" | The file is over 10 GB or 6 hours. Analyse a smaller proxy. |
| `AccessDenied` on `PutObject` | The role cannot write to `output_uri`. Check `<OUTPUT_BUCKET>` and `<OUTPUT_PREFIX>` in the policy. |
| "output_uri must be a folder" | End `output_uri` with `/`. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
