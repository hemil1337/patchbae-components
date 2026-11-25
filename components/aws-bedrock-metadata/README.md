# Amazon Bedrock Metadata

Writes descriptive metadata from a transcript: a title, a synopsis, keywords
and chapters. A model on Amazon Bedrock reads the transcript and writes the
metadata; you choose which model. Use it after a transcription step to fill
in the fields a catalogue or a publishing system asks for.

The result is written by a model. It can be wrong, and it should be reviewed
before it is published: put a **Manual approval** or **Human task** node
after this one.

## What it does

1. Takes the transcript from `text`, or reads it from the file at
   `transcript_uri`. For an Amazon Transcribe JSON it uses the transcript
   text in the file.
2. Sends the transcript to the Bedrock model in `model_id` and asks for the
   fields you listed, as JSON.
3. Returns the fields. When `output_uri` is set, it also writes them to a
   JSON file there.

A transcript is small text, so the node reads it itself. No audio or video
is read.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `transcript_uri` | one of the two | | S3 URI of the transcript: a plain text file (UTF-8) or the JSON that Amazon Transcribe writes. |
| `text` | one of the two | | The transcript itself, for example the `text` output of an Amazon Transcribe node. |
| `model_id` | yes | | The ID of a Bedrock model or inference profile, for example an Amazon Nova or Anthropic Claude model your account has access to. There is no default. |
| `fields` | no | `[title, synopsis, keywords, chapters]` | The metadata to write. Any of `title`, `synopsis`, `keywords`, `chapters`. |
| `language` | no | `same as the transcript` | The language to write in, such as `English` or `Swedish`. |
| `max_keywords` | no | `12` | Upper bound on the number of keywords, 1 to 50. |
| `max_input_chars` | no | `150000` | A transcript longer than this many characters is cut to this length, and `truncated` is `true`. |
| `output_uri` | no | | S3 folder to also write the metadata to. Must end with a slash, for example `s3://acme-media-work/metadata/`. |

Set exactly one of `transcript_uri` and `text`.

## Outputs

| Output | Description |
| --- | --- |
| `title` | A short title. |
| `synopsis` | A synopsis of 2 to 4 sentences. |
| `keywords` | A list of keywords, the most important first. |
| `chapters` | A list of chapters in order: `[{"start": "00:12:30", "title": "…"}]`. `start` is empty when the transcript has no timings. |
| `truncated` | `true` when the transcript was longer than `max_input_chars` and was cut. |
| `metadata_uri` | The JSON file with all of the above. Only when `output_uri` is set. |
| `model_id` | The model that wrote the metadata. |

Only the fields listed in `fields` are returned. When a file is written, the
node also adds it to the run's assets as `Metadata`, so a later node can use
`{{assets.Metadata.locator}}`.

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

Before you create the role, **enable access to the model in the Amazon
Bedrock console**, for the AWS account and the region the component runs in.
Without it Bedrock refuses every request, whatever the role allows.

You create the role once, in that account:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → Amazon Bedrock Metadata → Set up access**.

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
   `<REGION>`, `<ACCOUNT_ID>`, `<MODEL_ID>` and `<INFERENCE_PROFILE_ID>` with
   the region, your account and the model you use, and `<TRANSCRIPT_BUCKET>`,
   `<OUTPUT_BUCKET>` and `<OUTPUT_PREFIX>` with your own. The role can then
   call that one model, read the transcripts and write the metadata file,
   and nothing else.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

The policy names the model in two ways, and you keep what you use:

- If `model_id` is a model ID, keep the `foundation-model` line and remove
  the `inference-profile` line.
- If `model_id` is an inference profile ID, keep both. An inference profile
  that spans regions sends the request to the model in any of its regions,
  so the role needs the `foundation-model` line once for each of those
  regions.

If you never set `transcript_uri` or `output_uri`, remove the statement that
goes with it.

## How it runs

- **One request per run.** The transcript goes to the model in one request
  and the answer comes back as JSON. The node asks for at most 2,000 tokens
  of answer.
- **The transcript is content, not instructions.** It is sent inside
  `<transcript>` tags, and the model is told to describe what is inside them
  and never to act on it. A transcript cannot close the tags itself. This
  makes it unlikely that words spoken in a recording steer the model; it
  does not make it impossible, which is one more reason to review the
  result.
- **Long transcripts are cut.** Text beyond `max_input_chars` is not sent.
  The model is told it has the first part only, and `truncated` is `true`.
- **Chapter times come from the transcript.** The model can only give a
  chapter a start time if the transcript has timings, as an SRT or WebVTT
  file does. Plain text and the text of a Transcribe JSON have none, so
  `start` stays empty.
- **Safe to deliver twice.** The file is named `patchbae-<task>.json`. If
  the task is delivered again, the model is asked again and the same file is
  written over; there are never two. The wording can differ between the two
  answers.
- **An answer that is not JSON fails the node.** A code fence or a sentence
  around the JSON is accepted. Anything else fails with a message that says
  so, and nothing is written.
- **Time budget.** Up to 15 minutes for the one request. A long transcript
  takes the model longer to read than a short one.

## Cost

You pay Amazon Bedrock for the tokens the model reads and writes, at the
rate of the model you chose, plus S3 storage for the file. Reading the
transcript is most of the cost, so `max_input_chars` caps what one run can
cost. Patchbae adds no charge. See the Amazon Bedrock pricing page for the
model and your region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "Bedrock did not run the model …" with an access message | Enable access to the model in the Bedrock console for this account and region. Then check that `iam/policy.json` names this model and region. |
| "Bedrock did not run the model …" with a message that the model cannot be found | `model_id` is wrong for this region. Copy the ID from the Bedrock console. |
| A message that on-demand throughput is not supported for the model | The model is only offered through an inference profile. Set `model_id` to the inference profile ID. |
| A message that the input is too long | The transcript is larger than the model takes. Lower `max_input_chars` or choose a model that reads more. |
| "The model did not return JSON" | Run the node again. If it keeps happening, choose another model. |
| "The model's answer was cut off" | The answer did not fit. Ask for fewer `fields` or choose another model. |
| `chapters` have an empty `start` | The transcript has no timings. Give the node an SRT or WebVTT file instead of plain text. |
| "Set either transcript_uri or text, not both" | An earlier node filled both. Leave one of them empty. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
