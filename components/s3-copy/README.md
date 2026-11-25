# S3 Copy

Copies or moves one object, or everything under a folder, from one place on
S3 to another, in the same bucket or between buckets. Use it to put finished
media in a delivery bucket, to move originals to an archive bucket in a
cheaper storage class, or to clear a work folder at the end of a workflow.

## What it does

1. Looks at `source_uri`: one object, or a folder (it ends with `/`).
2. Asks S3 to copy each object to `destination_uri` in the storage class you
   chose. Content type, metadata and tags come along.
3. Checks every copy: it must exist and have the size of its source.
4. With `move`, deletes the sources once all copies have passed the check.
5. Returns where the copies are, how many there are and their total size.

The bytes never pass through Patchbae. S3 copies the objects itself, in parts
when they are large; the node only holds references.

How `destination_uri` is read:

| `source_uri` | `destination_uri` | Result |
| --- | --- | --- |
| `s3://in/a/show.mxf` | `s3://out/b/master.mxf` | `s3://out/b/master.mxf` |
| `s3://in/a/show.mxf` | `s3://out/b/` | `s3://out/b/show.mxf` (the file name is kept) |
| `s3://in/a/` | `s3://out/b/` | Every object under `a/` at the same path under `b/` |

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `source_uri` | yes | | S3 URI of an object, or of a folder ending with `/`. |
| `destination_uri` | yes | | S3 URI to copy to. For an object: a full key, or a folder ending with `/`. For a folder: a folder. |
| `move` | no | `false` | Delete the source after its copy has been checked. |
| `storage_class` | no | `STANDARD` | One of `STANDARD`, `STANDARD_IA`, `ONEZONE_IA`, `INTELLIGENT_TIERING`, `GLACIER_IR`, `GLACIER`, `DEEP_ARCHIVE`. |
| `max_objects` | no | `1000` | The most objects a folder may hold. A folder with more fails before anything is copied. |

## Outputs

| Output | Description |
| --- | --- |
| `destination_uri` | The copy, or the destination folder when a folder was copied. |
| `copied` | The number of objects copied. |
| `bytes` | Their total size in bytes. |
| `moved` | `true` when the sources were deleted. |
| `objects` | The S3 URIs of the copies, at most the first 100. |

When one object is copied, the node also adds the copy to the run's assets
as `Copy`, so a later node can use `{{assets.Copy.locator}}`.

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the buckets:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → S3 Copy → Set up access**.

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
   `<SOURCE_BUCKET>`, `<SOURCE_PREFIX>`, `<DESTINATION_BUCKET>` and
   `<DESTINATION_PREFIX>` with your own. A prefix may be empty to cover the
   whole bucket. What each statement is for:

   | Statement | Why |
   | --- | --- |
   | `ReadSource` | `s3:GetObject` reads the source. `s3:GetObjectTagging` reads its tags so the copy gets the same ones. |
   | `DeleteSourceOnlyNeededForMove` | `s3:DeleteObject` removes the source after a move. Leave this statement out if you only copy. |
   | `WriteAndCheckDestination` | `s3:PutObject` writes the copy and `s3:GetObject` checks it. `s3:PutObjectTagging` is used only for objects that carry tags. `s3:AbortMultipartUpload` cleans up when a large copy fails halfway. |
   | `ListBothBuckets` | `s3:ListBucket` lists a source folder and lets S3 say "not found" instead of "access denied" for a missing object. |

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

Buckets encrypted with your own KMS key also need `kms:Decrypt` on the
source's key and `kms:GenerateDataKey` on the destination's key in this role.
To copy between AWS accounts, the bucket in the other account must also allow
this role in its bucket policy.

## How it runs

- **Copy first, delete last.** In a move, no source is deleted until every
  copy exists and has the right size. If a copy fails, all sources are still
  there.
- **Safe to deliver twice.** A copy that runs again writes the same objects
  again. In a move, a source that is already gone while its copy exists
  counts as done, so a second delivery finishes what the first one started
  and never fails on what is already moved. If a move of a folder was cut off
  while deleting, `copied` and `objects` on the second delivery describe the
  objects that were still left to move.
- **Folders.** Objects are copied one after another. Placeholder objects
  whose key ends with `/` are skipped. Nothing is copied when the folder
  holds more than `max_objects` objects.
- **What comes along.** Content type, cache and encoding headers, metadata
  and tags. Not ACLs, and not older versions of a versioned object.
- **Versioned buckets.** A move puts a delete marker on the source; its older
  versions stay until a lifecycle rule removes them.
- **Time budget.** Up to 4 hours. Copies within a region are fast; copies
  between regions take longer.
- **Small pod.** The pod only makes API calls, so it needs little CPU and
  memory regardless of file size.

## Cost

You pay S3 for the requests, for storage of the copies in the storage class
you chose, and for data transfer when the buckets are in different regions.
Moving objects out of an infrequent-access or Glacier class before their
minimum storage period ends is billed for the remaining days. Patchbae adds
no charge. See the Amazon S3 pricing page for your region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `AccessDenied` on `HeadObject` or `403 Forbidden` | The role cannot read the object, or the object is missing and the role lacks `s3:ListBucket` on that bucket. |
| `AccessDenied` on `GetObjectTagging` | Add `s3:GetObjectTagging` on the source, as in `iam/policy.json`. |
| `AccessDenied` on `CopyObject`, `CreateMultipartUpload` or `UploadPartCopy` | The role cannot write to the destination. Check `<DESTINATION_BUCKET>` and `<DESTINATION_PREFIX>`, and the KMS key if the bucket has one. |
| `AccessDenied` on `DeleteObject` | The copy is done but the role may not delete the source. Add the `DeleteSourceOnlyNeededForMove` statement and run the node again. |
| "… is archived and cannot be read yet" | The source is in Glacier. Restore it first, for example with the S3 Glacier Restore component. |
| "source_uri holds more than … objects" | Raise `max_objects`, or copy a narrower folder. |
| "source_uri is a folder, so destination_uri must be a folder too" | End `destination_uri` with `/`. |
| "… does not exist" | Check `source_uri`. For a folder, end it with `/`. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
