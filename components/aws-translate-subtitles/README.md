# Amazon Translate Subtitles

Translates an SRT or WebVTT subtitle file on S3 into one or more languages.
Every cue keeps its number and its timing; only the text changes. Use it
after a transcription or subtitling step to make the same subtitles
available in the languages you publish in.

## What it does

1. Reads the subtitle file at `subtitle_uri`.
2. Sends the text of the cues to Amazon Translate, once for each language in
   `target_languages`.
3. Writes one translated file per language to `output_uri`, named
   `<name>.<language>.<format>`. `show.srt` translated to `sv` and `de`
   becomes `show.sv.srt` and `show.de.srt`.
4. Returns where the files were written, the number of cues, the source
   language and the format.

A subtitle file is small text, so the node reads it itself. No audio or
video is read.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `subtitle_uri` | yes | | S3 URI of the subtitle file. The name must end in `.srt` or `.vtt` and the file must be UTF-8 text. |
| `target_languages` | yes | | The languages to translate to, as Amazon Translate language codes, for example `[sv, de, fr]`. |
| `source_language` | no | `auto` | The language code of the subtitles, such as `en`, or `auto` to let Amazon Translate identify it. |
| `output_uri` | yes | | S3 folder for the translated files. Must end with a slash, for example `s3://acme-media-work/subtitles/`. |
| `formality` | no | `none` | `formal` or `informal` wording, or `none` to leave it to Translate. Amazon Translate supports formality for some target languages only; see its documentation. |

## Outputs

| Output | Description |
| --- | --- |
| `subtitle_uris` | One S3 URI per language: `{"sv": "s3://…/show.sv.srt", "de": "s3://…/show.de.srt"}`. |
| `cue_count` | The number of cues in the subtitle file. |
| `source_language` | The source language, as Amazon Translate reported it. With `auto`, this is the language it identified. |
| `format` | `srt` or `vtt`. The translated files have the same format as the source. |

The node also adds each file to the run's assets, labelled `Subtitles` and
the language code in capitals: `Subtitles SV`, `Subtitles DE`.

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the subtitle files:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → Amazon Translate Subtitles → Set up access**.

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
   `<SUBTITLE_BUCKET>`, `<OUTPUT_BUCKET>` and `<OUTPUT_PREFIX>` with your own.
   The role can then translate documents, read the subtitle files and write
   the translated files, and nothing else.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

With `source_language` set to `auto`, Amazon Translate asks Amazon Comprehend
which language the text is in. That is what the
`comprehend:DetectDominantLanguage` permission is for, and it needs a region
where Comprehend is available. If you always set `source_language`, you can
remove that statement.

## How it runs

- **Cues stay apart.** Each cue is sent to Translate as its own paragraph
  and read back by its position in the file, so text never moves from one
  cue to the next. If Translate leaves a cue out, the node fails and writes
  nothing.
- **Only the text changes.** Cue numbers, timings and WebVTT cue settings
  are copied as they are. So are the `WEBVTT` header and `NOTE`, `STYLE` and
  `REGION` blocks.
- **Line breaks follow the translation.** A two-line cue is sent as two
  lines and Translate decides where the break falls. Translated text can be
  longer than the original; check the line lengths if you have a limit.
- **Formatting tags are sent as text.** Tags inside a cue, such as `<i>` or
  a WebVTT `<v Name>`, are part of the text Translate receives. Check them
  in the result if your subtitles rely on them.
- **Safe to deliver twice.** The file names depend only on the source file
  name and the language. If the task is delivered again, the files are
  translated again and written over the same keys; there are never two sets.
- **All or nothing.** Every language is translated before any file is
  written.
- **Large files.** Translate takes 100 KB per request, so a long file is
  sent in several requests. The language is identified in the first one and
  named in the rest.
- **Time budget.** Up to 30 minutes. The subtitles of a feature film are
  two or three requests per language.

## Cost

You pay Amazon Translate per character, once for each target language, plus
S3 storage for the results. Patchbae adds no charge. See the Amazon
Translate pricing page for real-time document translation in your region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `AccessDenied` on `TranslateDocument` | The role is missing `translate:TranslateDocument` from `iam/policy.json`. |
| `AccessDenied` on `GetObject` | The role cannot read the subtitle file, or the file is not there: S3 answers `AccessDenied` for a missing file when the role may not list the bucket. |
| An error that names Comprehend or automatic language detection | `auto` needs `comprehend:DetectDominantLanguage` and a region with Amazon Comprehend. Or set `source_language`. |
| "Amazon Translate could not translate from … to …" | Translate does not offer that language pair, or a code is wrong. Check the codes against the Amazon Translate language list. |
| "The subtitle file is not UTF-8 text" | Save the file as UTF-8. Older SRT files are often in a Windows encoding. |
| "The file does not look like SRT" or "… WebVTT" | The file extension and the content do not match, or the file is not a subtitle file. |
| "returned no text for … cues" | Run the node again. If the same cue fails every time, look at that cue in the source file. |
| "output_uri must be a folder" | End `output_uri` with `/`. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
