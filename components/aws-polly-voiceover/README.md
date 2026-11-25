# Amazon Polly Voice-over

Turns text into a spoken audio file on S3, using an Amazon Polly voice. Use
it to make a voice-over from a script, an audio version of an article, or a
spoken track from text an earlier node or an agent wrote.

## What it does

1. Takes the words to speak from `text`, or reads them from the file at
   `text_uri`.
2. Starts an Amazon Polly speech synthesis task with the voice and engine
   you chose.
3. Waits for the task to finish.
4. Returns where the audio file was written, the number of characters
   synthesized, the voice and the Polly task.

Polly writes the audio file to S3 itself. The audio never passes through
Patchbae; the node only holds a reference to it.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `text` | one of the two | | The words to speak. |
| `text_uri` | one of the two | | S3 URI of a `.txt` or `.ssml` file with the words to speak, as UTF-8 text. An `.ssml` file is read as SSML. |
| `voice_id` | no | `Joanna` | The Polly voice, such as `Joanna`, `Matthew` or `Elin`. The voice decides the language and the accent. |
| `engine` | no | `neural` | `neural`, `standard`, `long-form` or `generative`. Each voice supports some engines only, and not every engine is in every region. |
| `language_code` | no | | Only for a voice that speaks two languages, to choose between them, for example `en-IN` or `hi-IN`. |
| `text_type` | no | `text` | `text`, or `ssml` when the text is SSML markup inside `<speak>…</speak>`. |
| `output_format` | no | `mp3` | `mp3`, `ogg_vorbis` or `pcm`. `pcm` is raw 16-bit mono audio without a header. |
| `output_uri` | yes | | S3 folder for the audio file. Must end with a slash, for example `s3://acme-media-work/voice-over/`. |

Set exactly one of `text` and `text_uri`. The text can hold up to 200,000
characters.

## Outputs

| Output | Description |
| --- | --- |
| `audio_uri` | The audio file. |
| `characters` | The number of characters Polly synthesized, which is what it bills. |
| `voice_id` | The voice that was used. |
| `task_id` | The Polly speech synthesis task, to look it up in AWS. |

The node also adds the file to the run's assets as `Voice-over`, with the
media type `audio/mpeg`, `audio/ogg` or `audio/pcm`.

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the output bucket:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → Amazon Polly Voice-over → Set up access**.

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
   `<OUTPUT_BUCKET>` and `<OUTPUT_PREFIX>` with your own, and `<TEXT_BUCKET>`
   with the bucket that holds your text files. The role can then start and
   read speech synthesis tasks, write the audio, look for audio it already
   wrote and read the text files, and nothing else.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

Polly writes the audio file with this role's permissions, which is why the
role itself needs `s3:PutObject` on the output folder. If you never use
`text_uri`, remove the `ReadTextFile` statement. Use an output bucket in the
region the component runs in.

## How it runs

- **One audio file per task.** Polly names the file
  `patchbae-<task>.<Polly task>.<extension>` in the output folder. The
  extension is `mp3`, `ogg` or `pcm`.
- **Safe to deliver twice.** Polly has no way to say "this is the same
  request as before". So before it starts anything, the node looks in the
  output folder for a file from the same task and returns that one instead
  of synthesizing (and paying) again. This covers a task that is delivered
  again after Polly has finished. If it is delivered again while Polly is
  still working, the file is not there yet and a second synthesis starts.
- **Time budget.** Up to 1 hour. The longer the text, the longer Polly
  takes. If the node is stopped while it waits, Polly still finishes the
  task and the next delivery finds the file.
- **Small pod.** The pod only makes API calls, so it needs little CPU and
  memory regardless of the length of the text.

## Cost

You pay Amazon Polly per character synthesized, at a rate that depends on
the engine, plus S3 storage for the audio. Patchbae adds no charge. See the
Amazon Polly pricing page for your region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `AccessDenied` on `StartSpeechSynthesisTask` | The role is missing the `polly:*` actions in `iam/policy.json`. |
| `AccessDenied` on `ListObjectsV2` | The role is missing `s3:ListBucket` on the output bucket. |
| "Polly did not accept the request: This voice does not support the selected engine" | Choose an engine the voice supports. The Polly voice list shows the engines for each voice. |
| "Polly did not accept the request" with an SSML message | The text is not valid SSML. Wrap it in `<speak>…</speak>`, or set `text_type` to `text`. |
| "Polly did not accept the request" with a message about text length | Polly limits the characters it bills in one task. Split the text over several nodes. |
| "Polly could not synthesize the text" with a message about S3 | The role cannot write to the output folder, or the bucket is in another region. |
| "Set either text or text_uri, not both" | An earlier node filled both. Leave one of them empty. |
| "output_uri must be a folder" | End `output_uri` with `/`. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
