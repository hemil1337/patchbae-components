# Amazon Transcribe

Turns the speech in an audio or video file on S3 into a transcript and, if you
want them, SRT and WebVTT subtitle files. Use it after ingest to make content
searchable, to give an agent the spoken words to work with, or as the first
step of a subtitling workflow.

## What it does

1. Starts an Amazon Transcribe job for the file at `media_uri`.
2. Waits for the job to finish.
3. Returns where the transcript and the subtitle files were written, the
   language, and (optionally) the plain transcript text.

The media file never passes through Patchbae. Transcribe reads it from S3 and
writes its results to S3; the node only holds references.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `media_uri` | yes | | S3 URI of the audio or video file. MP3, MP4, WAV, FLAC, OGG, AMR, WebM and M4A are supported, up to 4 hours or 2 GB. |
| `output_uri` | yes | | S3 folder for the results. Must end with a slash, for example `s3://acme-media-work/transcripts/`. |
| `language_code` | no | `auto` | A language code such as `en-US` or `sv-SE`, or `auto` to let Transcribe identify the language. |
| `language_options` | no | | With `auto`: the languages to choose between. Two or more. |
| `subtitle_formats` | no | `[srt, vtt]` | Subtitle files to write. Any of `srt`, `vtt`; an empty list for none. |
| `speaker_labels` | no | `false` | Label who speaks when. |
| `max_speakers` | no | `4` | Upper bound on speakers, 2 to 30. Used with `speaker_labels`. |
| `vocabulary_name` | no | | A custom vocabulary in the same region. Needs a fixed `language_code`. |
| `include_text` | no | `true` | Put the plain transcript text in the node output. |

## Outputs

| Output | Description |
| --- | --- |
| `transcript_uri` | The transcript JSON with words, timings and confidence. |
| `subtitle_uris` | One S3 URI per subtitle format: `{"srt": "s3://…", "vtt": "s3://…"}`. |
| `language_code` | The language the file was transcribed in. |
| `text` | The plain transcript, cut at 200,000 characters. |
| `text_truncated` | `true` when `text` was cut. |
| `job_name` | The Transcribe job name, to find it in the AWS console. |

The node also adds the files to the run's assets as `Transcript`,
`Subtitles SRT` and `Subtitles VTT`, so a later node can use
`{{assets.Transcript.locator}}`.

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
   **Components → Amazon Transcribe → Set up access**.

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
   The role can then start and read transcription jobs, read the media and
   write the results, and nothing else.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

The media bucket must be in the region the component runs in: Transcribe only
reads from buckets in its own region.

## How it runs

- **One job per task.** The job is named after the task, so if the task is
  delivered twice the second delivery waits for the first job instead of
  starting (and paying for) another one. A job that failed on an earlier
  attempt is removed before the retry.
- **Time budget.** Up to 4 hours. A file usually takes a fraction of its own
  duration. If the node is stopped, the Transcribe job keeps running in AWS
  and a retry picks it up.
- **Small pod.** The pod only makes API calls, so it needs little CPU and
  memory regardless of file size.

## Cost

You pay Amazon Transcribe per second of audio, plus S3 storage for the
results. Patchbae adds no charge. See the Amazon Transcribe pricing page for
your region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `AccessDenied` on `StartTranscriptionJob` | The role is missing the `transcribe:*` actions in `iam/policy.json`. |
| "The S3 URI … can't be accessed" | The role cannot read the media file, or the bucket is in another region. |
| "output_uri must be a folder" | End `output_uri` with `/`. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
