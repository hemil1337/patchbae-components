# Patchbae components

Components for Patchbae, the workflow platform for media operations. A component is a piece of software packaged as
a workflow node: Patchbae reads this repository, shows every component under
**Components**, and an installed component can be dropped onto any workflow.

Each run of a component node is its own pod. The pod assumes an IAM role you
create for that component, does its work against AWS, and returns references
to what it produced.

- [Use the library in Patchbae](#use-the-library-in-patchbae)
- [What is in the library](#what-is-in-the-library)
- [Build a new component](#build-a-new-component)
- [Reference](#reference): [layout](#layout), [manifest](#the-manifest), [code](#the-code), [rules](#rules-every-component-follows), [AWS access](#aws-access), [tests](#tests-without-an-aws-account)
- [Contribute](#contribute)

## Use the library in Patchbae

1. In Patchbae, open **Settings → Components library**.
2. Add this repository's URL: `https://github.com/hemil1337/patchbae-components`.
   Leave the branch empty for the default branch, or name a tag to stay on a
   fixed version.
3. Sync. Every component appears under **Components**.
4. Install a component, then follow **Set up access** on its page to give it
   an IAM role. The component's README has the trust policy and the
   permissions.

A component that fails validation is reported at sync and skipped. The rest
of the library still syncs.

## What is in the library

| Component | What it does | AWS services |
| --- | --- | --- |
| [`aws-transcribe`](components/aws-transcribe) | Speech to transcript and subtitles | Transcribe, S3 |
| [`aws-mediaconvert`](components/aws-mediaconvert) | Transcode to MP4, HLS or a job template | MediaConvert, S3 |
| [`aws-rekognition-video`](components/aws-rekognition-video) | Labels, moderation and shot detection for video | Rekognition, S3 |
| [`aws-translate-subtitles`](components/aws-translate-subtitles) | Translate SRT and WebVTT subtitles | Translate, S3 |
| [`aws-polly-voiceover`](components/aws-polly-voiceover) | Text to a spoken audio file | Polly, S3 |
| [`aws-bedrock-metadata`](components/aws-bedrock-metadata) | Synopsis, keywords and chapters from a transcript | Bedrock, S3 |
| [`aws-medialive`](components/aws-medialive) | Start or stop a live channel, insert an ad break, switch input | MediaLive |
| [`aws-mediaconnect`](components/aws-mediaconnect) | Start or stop a contribution flow and return its ingest address | MediaConnect |
| [`aws-mediapackage`](components/aws-mediapackage) | Harvest a live clip to S3, publish a VOD asset with playback URLs | MediaPackage, S3 |
| [`aws-mediatailor`](components/aws-mediatailor) | Set up ad insertion for a stream, schedule a program on a channel | MediaTailor |
| [`aws-elemental-inference`](components/aws-elemental-inference) | Live AI feeds: vertical crops, clips, subtitles, metadata | Elemental Inference |
| [`aws-ivs`](components/aws-ivs) | Create a low-latency channel, read or stop its stream, send timed metadata | IVS |
| [`aws-kinesis-video-streams`](components/aws-kinesis-video-streams) | Export a clip or still images from a video stream to S3 | Kinesis Video Streams, S3 |
| [`aws-deadline-cloud`](components/aws-deadline-cloud) | Submit a render job and wait for it | Deadline Cloud, S3 |
| [`s3-copy`](components/s3-copy) | Copy or move media between buckets | S3 |
| [`s3-glacier-restore`](components/s3-glacier-restore) | Bring archived media back from Glacier | S3 |

## Build a new component

The example builds `aws-example`, a component that calls one AWS service.
Every step names the file it makes. The [reference](#reference) below has
the details of each file.

### 1. Fork and set up

Fork this repository, clone your fork, and make a branch. `main` only takes
pull requests.

```bash
git clone https://github.com/<you>/patchbae-components
cd patchbae-components
git checkout -b aws-example
python3 -m pip install -r requirements-test.txt
python3 -m pytest          # everything passes before you start
```

### 2. Make the folder

The folder's name is the component's name: lowercase letters, digits and
dashes, 3 to 40 characters. It never changes, because workflows refer to it.

```bash
mkdir -p components/aws-example/{src,iam,tests}
```

### 3. Write the manifest: `component.yaml`

Say what the component is, what it takes and what it returns. Copy the
manifest of a component close to yours and change it. The
[manifest reference](#the-manifest) lists every field.

Decide the inputs and outputs before you write code. They are the
component's contract with every workflow that uses it.

### 4. Write the code: `src/main.py`

One function, `run(inputs)`, that returns a dict whose keys are the outputs.

```python
import boto3
from botocore.exceptions import ClientError

import patchbae


def run(inputs):
    media_uri = str(inputs.get("media_uri") or "").strip()
    if not media_uri.startswith("s3://"):
        patchbae.fail(f"media_uri must be an S3 URI such as s3://bucket/file.mp4, got {media_uri!r}")

    service = boto3.client("example")
    job_name = f"patchbae-{patchbae.task.id}"       # the same on a second delivery
    ...
    patchbae.log("finished", job=job_name)
    return {"result_uri": result_uri}
```

Follow the six [rules](#rules-every-component-follows). The two that take
the most thought:

- **Safe to deliver twice.** Name the job after `patchbae.task.id` and look
  for it before you start it.
- **Check inputs first.** Fail with a sentence the workflow builder can act
  on, before anything is started or billed.

### 5. Write the permissions: `iam/policy.json`

List only the actions the code calls. Scope resources to placeholders in
angle brackets, such as `<MEDIA_BUCKET>` and `<ACCOUNT_ID>`, that the
operator replaces. No `s3:*`.

### 6. Write the README: `README.md`

Patchbae shows it on the component's page. It needs these sections, with
these headings: **What it does**, **Inputs**, **Outputs**, **AWS access**,
**How it runs**, **Troubleshooting**. **AWS access** holds the trust policy
from [AWS access](#aws-access) below, the steps to create the role, and
anything extra the service needs, such as a service role to pass. Add
**Cost** when a run costs money.

### 7. Add a logo: `logo.svg`

A square SVG, at most 64 KiB, with no script. It is shown on the card.

### 8. Write the tests: `tests/test_main.py`

Use `FakeAws`, so the tests fail when a call does not match the AWS API.
See [tests without an AWS account](#tests-without-an-aws-account). Cover at
least: the normal run, a second delivery of the same task, a failure the
service reports, and every input the code rejects.

### 9. Check and open a pull request

```bash
python3 -m pytest                          # every manifest and every component
python3 -m pytest components/aws-example   # only yours
```

`tests/test_manifests.py` checks your manifest, README and policy against the
rules. When everything passes, push the branch and open a pull request.
Say in it whether the component has run against a real AWS account.

You can also write a component inside Patchbae, with **Components → Create
with Claude**, and bring the files here.

## Reference

### Layout

```text
library.yaml                     the library's name and description
components/
  <name>/
    component.yaml               the manifest: what it is, what it takes and returns
    README.md                    shown in Patchbae; includes the IAM role setup
    logo.svg                     square logo shown on the card
    src/main.py                  the code
    iam/policy.json              the permissions the component's role needs
    tests/                       unit tests (not installed)
tests/test_manifests.py          checks every component against the rules below
tests/test_fake_aws.py           checks the AWS stand-in the component tests use
requirements-test.txt            what the tests need installed
sdk/python/patchbae/             local stand-in for the patchbae module, for tests
```

Only `component.yaml`, `README.md` and the entrypoint are required.

A component can be at most 64 files and 1 MiB, with no file above 256 KiB.
The folder that holds the entrypoint travels to the pod and can be at most
800 KiB.

### The manifest

```yaml
apiVersion: patchb.ae/v1
kind: Component
name: aws-transcribe             # the folder name: lowercase letters, digits, dashes; 3 to 40 characters
displayName: Amazon Transcribe   # shown on the card, at most 80 characters
summary: Turn the speech in an audio or video file on S3 into a transcript and subtitles.
version: 1.0.0                   # semantic version; raise it with every change
category: speech
tags: [aws, transcribe, subtitles]
logo: logo.svg
license: Apache-2.0
maintainers:
  - name: Patchbae

runtime:
  language: python               # python
  entrypoint: src/main.py        # file that holds the handler
  handler: run                   # function called with the node's inputs
  packages:                      # pinned dependencies, baked into the image at install
    - boto3==1.35.99
  timeoutSeconds: 14400          # time budget for one run, 30 to 21600
  resources:
    cpu: 250m                    # at most 4
    memory: 256Mi                # at most 8Gi

aws:
  services: [transcribe, s3]     # shown as chips; lowercase service names
  policy: iam/policy.json        # permissions the role needs

inputs:
  - name: media_uri
    label: Media file
    type: s3_uri
    required: true
    description: The audio or video file to transcribe.
    example: "{{assets.Original.locator}}"

outputs:
  - name: transcript_uri
    type: s3_uri
    description: The transcript JSON.
```

| Field | Rules |
| --- | --- |
| `name` | Must equal the folder name. Never changes: workflows refer to it. |
| `summary` | One sentence, at most 140 characters. It is the card's description. |
| `version` | `MAJOR.MINOR.PATCH`. Raise MAJOR when an input or output is removed or changes meaning. |
| `category` | One of `ingest`, `transcode`, `speech`, `vision`, `language`, `qc`, `live`, `render`, `storage`, `delivery`, `utility`. |
| `runtime.packages` | Exact versions (`name==1.2.3`), at most 20. Nothing is installed while a node runs. |
| `runtime.timeoutSeconds` | The pod is stopped when it runs out. Pick the longest a healthy run can take. |
| `aws` | Leave the whole block out for a component that calls no AWS service. It then needs no role. |
| `inputs[].type` | `string`, `text`, `integer`, `number`, `boolean`, `enum` (with `options`), `list`, `json`, `s3_uri`. |
| `inputs[].default` | Used when the workflow builder leaves the input empty. A `required` input has no default. |
| `outputs` | What the handler returns. The workflow builder offers these to later nodes. |

Input and output names are `snake_case`. A component has at most 60 inputs.

A component with several actions, such as `aws-medialive`, takes an `action`
input of type `enum` and returns every output on every run, with empty
values for the ones the action does not produce.

### The code

- The handler gets one dict: the node's inputs, with defaults applied and
  types checked against the manifest.
- It returns a JSON object. Its keys are the component's outputs.
- `patchbae.log(message, **fields)` and `patchbae.warn(...)` write to the
  node's log.
- `patchbae.fail(message)` fails the node with a clean message. Use it for
  problems the workflow builder can fix. Let unexpected errors raise.
- `patchbae.emit_asset(label, locator, media_type=None, **metadata)` adds a
  labelled media reference to the run, readable by later nodes as
  `{{assets.<label>.locator}}`.
- `patchbae.task.id` identifies the task and stays the same when the task is
  delivered again.
- AWS calls use plain `boto3`. Credentials and region come from the pod; never
  pass keys.

### Rules every component follows

These are what make a component safe to run thousands of times a day.

1. **References, not payloads.** Take S3 URIs in and return S3 URIs. Let the
   AWS service read and write S3 itself. Do not download media into the pod
   unless the work has to happen there.
2. **Safe to deliver twice.** A task can be delivered more than once. Derive
   job names and idempotency tokens from `patchbae.task.id`, and look for an
   existing job before starting one, so a second delivery waits for the first
   job instead of paying for another.
3. **Wait politely.** Poll with a growing delay (start at 5 seconds, cap at
   30) and a little jitter. The pod's time budget ends the wait.
4. **Least privilege.** `iam/policy.json` lists only the actions the code
   calls, scoped to placeholders such as `<MEDIA_BUCKET>` that the operator
   fills in. No `s3:*`, no `*` resources where the service supports scoping.
5. **No credentials in inputs.** Access comes from the role. A component
   never takes a key, token or password as an input.
6. **Say what is wrong.** Check inputs first and fail with a sentence the
   workflow builder can act on, before anything is started or billed.

### AWS access

A component with an `aws` block needs an IAM role. Patchbae creates one
Kubernetes service account per installed component, and the component's pods
run as it. The role's trust policy names that service account, so only that
component can assume the role:

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

Patchbae shows this policy with the namespace and service account filled in
when you set a component up. Every component's README repeats the steps and
lists anything extra it needs, such as a service role to pass to
MediaConvert.

### Tests without an AWS account

Component tests replace `boto3.client` with a stand-in and import the local
`patchbae` module from `sdk/python`. Nothing needs an AWS account.

Because no test reaches AWS, a misspelled parameter would pass unnoticed. Use
`FakeAws` from `patchbae.testing` as the stand-in: it checks every call, and
every response a test makes up, against the service models that ship with
the AWS SDK.

```python
import patchbae
from patchbae.testing import FakeAws, load_component

main = load_component(__file__)      # loads src/main.py the way the runner does


def test_starts_the_channel(monkeypatch):
    aws = FakeAws()
    medialive = aws.client("medialive")
    medialive.respond("describe_channel", {"Id": "1234567", "State": "IDLE"}, {"Id": "1234567", "State": "RUNNING"})
    medialive.respond("start_channel", {"State": "STARTING"})
    monkeypatch.setattr(main.boto3, "client", aws.client)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    patchbae.reset()

    main.run({"channel_id": "1234567", "action": "start_channel"})

    assert medialive.called("start_channel") == [{"ChannelId": "1234567"}]
```

| You write | What it does |
| --- | --- |
| `client.respond("operation", first, second)` | Sets what the operation returns, call by call. The last response repeats. |
| `FakeAws.error("NotFoundException", status=404)` | A response that raises the error boto3 raises for that code. |
| `client.called("operation")` | The parameters of every call, in order. |
| `patchbae.logged`, `patchbae.emitted_assets` | What the component logged and emitted. `patchbae.reset()` clears them. |
| `pytest.raises(patchbae.ComponentFailed, match="...")` | Expects `patchbae.fail(...)`. |

A call fails the test when the operation does not exist, a parameter is
unknown, missing, of the wrong type or not a value the API lists, or when no
response was set up for it. A mocked response fails the test when it has a
field the API does not return or a value of the wrong type. The check uses
the SDK version in `requirements-test.txt`. A component that pins an older
SDK can still call something its own version lacks, so run its tests once
with that version installed.

## Contribute

- **`main` is protected.** Nobody pushes to it directly. Every change comes
  in through a pull request from a fork or a branch.
- **The `tests` check runs on every pull request** and has to pass.
- **One component per pull request** keeps reviews short.
- **Raise the component's `version`** with every change to it.
- **Found a problem with a component in a real AWS account?** Open an issue
  with the component's name, the region and the node's log.

## Licence

Apache License 2.0. See [LICENSE](LICENSE).
