# S3 Glacier Restore

Makes an archived object on S3 readable again. Objects in S3 Glacier Flexible
Retrieval, S3 Glacier Deep Archive and the archive tiers of S3
Intelligent-Tiering cannot be read until they are restored. Put this node
before a transcode, a copy or any other node that has to read an archived
original.

## What it does

1. Looks at the object at `media_uri`.
2. If it is not archived, or already restored, returns at once with status
   `available`.
3. Otherwise asks S3 to restore it with the retrieval tier you chose.
4. With `wait`, waits until the object can be read and returns `available`.
   Without `wait`, returns `in_progress` straight away.

The object never passes through Patchbae. S3 restores it in place and the
node returns the same `media_uri` it was given.

How long a restore takes depends on where the object is and on the tier:

| Tier | S3 Glacier Flexible Retrieval | S3 Glacier Deep Archive |
| --- | --- | --- |
| `Expedited` | 1 to 5 minutes | not available |
| `Standard` | 3 to 5 hours | up to 12 hours |
| `Bulk` | 5 to 12 hours | up to 48 hours |

**A run cannot wait longer than 6 hours.** A Standard restore from Glacier
Flexible Retrieval fits. A Standard restore from Deep Archive and any Bulk
restore may not. For those, set `wait` to `false`: the node asks for the
restore and returns `in_progress`. Later in the workflow, run the node again
on the same file (for example after a delay, or on a schedule) to see whether
it is `available`. Running it again is safe: a restore that is in progress is
never asked for twice.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `media_uri` | yes | | S3 URI of the object to restore. |
| `tier` | no | `Standard` | `Standard`, `Bulk` or `Expedited`. `Expedited` is not available for Deep Archive. |
| `days` | no | `7` | How many days the restored copy stays readable. Not used for Intelligent-Tiering, where the object comes back for good. |
| `wait` | no | `true` | Wait until the object can be read, for at most 6 hours. |

## Outputs

| Output | Description |
| --- | --- |
| `media_uri` | The same object, for the next node to read. |
| `status` | `available` when the object can be read now, `in_progress` when the restore has been asked for and is not finished. |
| `was_archived` | `false` when the object was not in an archive class and nothing had to be done. |
| `storage_class` | The object's storage class, for example `GLACIER`, `DEEP_ARCHIVE` or `STANDARD`. |
| `expires_at` | When the restored copy goes away, as an ISO 8601 time such as `2026-12-01T00:00:00+00:00`. Empty while the restore runs, for objects that are not archived and for Intelligent-Tiering. |

A restore does not change the object's storage class. It adds a temporary
readable copy; after `expires_at` the object is archived only again. To keep
it readable, copy it to another storage class, for example with the S3 Copy
component.

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
   **Components → S3 Glacier Restore → Set up access**.

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
   `<MEDIA_BUCKET>` and `<MEDIA_PREFIX>` with your own; the prefix may be
   empty to cover the whole bucket. The role can then ask for restores
   (`s3:RestoreObject`) and look at an object's state (`s3:GetObject`, which
   is what a status check needs), and nothing else. `s3:ListBucket` lets S3
   say "not found" instead of "access denied" when `media_uri` is wrong.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

## How it runs

- **One restore per object.** Before asking for anything the node looks at
  the object. If a restore is already running, from an earlier delivery of
  the task or from anyone else, the node waits for that one instead of asking
  (and paying) again.
- **Waiting.** The node checks the object once a minute at first, backing off
  to once every five minutes.
- **Time budget.** Up to 6 hours. If the node is stopped while it waits, the
  restore carries on in S3. A retry, or a later run with the same
  `media_uri`, picks it up.
- **Already restored.** An object whose restored copy is still there returns
  `available` with its current `expires_at`. The node does not extend it.
- **Small pod.** The pod only makes API calls, so it needs little CPU and
  memory regardless of file size.

## Cost

You pay S3 for the retrieval, per request and per GB, at a price that depends
on the tier: Expedited costs the most and Bulk the least. You also pay
standard storage for the restored copy for `days` days, on top of the archive
storage. Patchbae adds no charge. See the Amazon S3 pricing page for your
region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| The node times out after 6 hours | The restore takes longer than a run may wait. Set `wait` to `false` and check again later in the workflow. The restore itself is not lost. |
| "Expedited is not available for objects in Deep Archive" | Set `tier` to `Standard` or `Bulk`. |
| "S3 has no Expedited capacity right now" | Run the node again later, or use `Standard`. |
| `AccessDenied` on `RestoreObject` | The role is missing `s3:RestoreObject` on the object. Check `<MEDIA_BUCKET>` and `<MEDIA_PREFIX>` in the policy. |
| `403 Forbidden` on `HeadObject` | The role lacks `s3:GetObject` on the object, or the object is missing and the role lacks `s3:ListBucket`. |
| "… does not exist" | Check `media_uri`. |
| A later node still cannot read the file | The status was `in_progress`. Wait for `available` before reading. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
