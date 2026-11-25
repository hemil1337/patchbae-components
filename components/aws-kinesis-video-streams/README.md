# Amazon Kinesis Video Streams

Takes stored media out of Amazon Kinesis Video Streams and puts it on S3,
where the rest of a workflow can use it. Export the seconds around an event
from a camera or contribution stream as an MP4 clip, or as still images for
thumbnails and analysis.

## What it does

| Action | What happens |
| --- | --- |
| `export_clip` | Writes the time range to `output_uri` as one MP4 file. |
| `extract_images` | Writes one still image per sampling interval to a folder under `output_uri`. |

Kinesis Video Streams has no way to write to S3 itself. This is the one
component in the library where the media passes through the pod. A clip is
streamed from Kinesis straight into S3 in parts of 8 MB; it is never held in
memory or written to disk as a whole.

One request to Kinesis returns at most 100 MB or 200 fragments of video. A
range that holds more comes back cut short or is refused, so keep clips to a
few minutes and use several nodes for more.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `stream_name` | yes | | Name of the stream. |
| `action` | yes | | `export_clip` or `extract_images`. |
| `start_time` | yes | | Start of the range, a UTC time such as `2026-11-25T19:42:10Z`. |
| `end_time` | yes | | End of the range. At most 24 hours later for a clip, 300 seconds for images. |
| `output_uri` | yes | | S3 folder. Must end with a slash. |
| `file_name` | no | `patchbae-<task id>` | Name of the clip without extension, or of the images' folder. |
| `timestamp_type` | no | `PRODUCER_TIMESTAMP` | `PRODUCER_TIMESTAMP` (the camera's clock) or `SERVER_TIMESTAMP` (when Kinesis received the media). |
| `image_format` | no | `JPEG` | For `extract_images`: `JPEG` or `PNG`. |
| `sampling_interval_seconds` | no | `5` | For `extract_images`: seconds between images. At least 0.2. |
| `max_images` | no | `100` | For `extract_images`: stop after this many. 1 to 1000. |
| `image_width`, `image_height` | no | | For `extract_images`: size in pixels. Set both or neither. |

## Outputs

Outputs an action does not produce are empty or `0`.

| Output | From | Description |
| --- | --- | --- |
| `clip_uri` | `export_clip` | The MP4 clip: `<output_uri><file_name>.mp4`. |
| `clip_bytes` | `export_clip` | The clip's size in bytes. |
| `images_uri` | `extract_images` | The folder with the images: `<output_uri><file_name>/`. |
| `image_uris` | `extract_images` | The images in time order. Each is named after its timestamp. |
| `image_count` | `extract_images` | How many images were written. |
| `skipped_count` | `extract_images` | Sampling points without usable media. |

The node adds the clip to the run's assets as `Clip` and the image folder as
`Stills`, so a later node can use `{{assets.Clip.locator}}`.

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the stream and the bucket:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → Amazon Kinesis Video Streams → Set up access**.

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
   `<REGION>`, `<ACCOUNT_ID>`, `<STREAM_NAME>`, `<OUTPUT_BUCKET>` and
   `<OUTPUT_PREFIX>` with your own. The role can then read stored media of
   that one stream and write under that one prefix, and nothing else. It
   cannot read from the bucket, and it cannot send media to the stream.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

A stream encrypted with your own KMS key also needs `kms:Decrypt` on that
key, and a bucket encrypted with one needs `kms:GenerateDataKey`.

## How it runs

- **Safe to deliver twice.** The clip and the images are named after the
  task (or `file_name`) and the media's timestamps. A second delivery writes
  the same files again with the same content.
- **Small memory.** A clip moves through the pod two 8 MB parts at a time.
  Images are a few hundred kilobytes each and are written one by one.
- **Time budget.** 30 minutes.
- **The stream must keep its data.** A stream with a retention of 0 hours
  has nothing stored to export.

## Cost

Kinesis Video Streams bills the gigabytes read from a stream, and
`extract_images` by the number of images made. S3 bills the storage.
Patchbae adds no charge. See the Amazon Kinesis Video Streams pricing page.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "Stream … was not found" | The name is wrong or the component is set up for another region. |
| "Stream … has no media between …" | Nothing was recorded in that range, the range is in the other kind of timestamp (`timestamp_type`), or the stream's retention has passed. |
| "Kinesis Video Streams could not make a clip of this range: …" | Kinesis's reason follows. A clip needs H.264 or H.265 video with codec private data in every fragment. |
| The clip is shorter than the range | The range holds more than 100 MB or 200 fragments. Export it in several shorter nodes. |
| "The range from start_time to end_time is too long" | 24 hours for a clip, 300 seconds for images. |
| `AccessDenied` on `PutObject` | `<OUTPUT_BUCKET>` or `<OUTPUT_PREFIX>` in the policy does not match `output_uri`. |
| `AccessDenied` on a `kinesisvideo:` action | The component's role is missing actions from `iam/policy.json`, or the placeholders were not replaced. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
