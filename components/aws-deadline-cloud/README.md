# AWS Deadline Cloud

Submits a render job to AWS Deadline Cloud, the managed render farm, and
waits for it. The job is described by an Open Job Description template, the
same file artists submit from Blender, Maya, Houdini, Nuke or After Effects.
Use it to render graphics, promos and VFX shots as a step of a workflow.

## What it does

1. Reads the job template, from S3 or from the node's input.
2. Submits it to the queue with the parameter values you give.
3. Waits until every task has finished, and fails the node when the job
   fails.

The rendering happens on the farm's workers. They fetch the scene and write
the frames themselves, to S3 or to shared storage, as the template's steps
say. No media passes through Patchbae. The template is a small text file and
is the only thing the pod reads from S3.

This component does not build job attachments. A template whose steps read
their inputs from S3 or from a file system the workers mount needs none. For
a scene with local assets that have to be uploaded as job attachments, submit
from the artist's application or the Deadline Cloud CLI.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `farm_id` | yes | | The farm's ID, `farm-` and 32 characters. |
| `queue_id` | yes | | The queue's ID, `queue-` and 32 characters. |
| `template_uri` | one of the two | | S3 URI of a job template ending in `.json`, `.yaml` or `.yml`. |
| `template` | one of the two | | The job template inline, as JSON or YAML. |
| `parameters` | no | | Values for the template's parameters, as a JSON object. |
| `priority` | no | `50` | 0 to 100. Higher runs first. |
| `name` | no | | Name for the job. Empty means the template's name. |
| `max_failed_tasks` | no | | The job fails once this many tasks have failed. |
| `max_retries_per_task` | no | | How often a failed task is tried again. |
| `max_workers` | no | | The most workers on the job at once. |
| `storage_profile_id` | no | | The storage profile that maps the job's paths, `sp-` and 32 characters. |
| `wait` | no | `true` | Wait until the job has finished. |

Parameter values are written the natural way, and the node tags each with
its kind for the API:

| You write | Sent as |
| --- | --- |
| `"Frames": "1-250"` | a string parameter |
| `"Samples": 128` | an integer parameter |
| `"Gamma": 2.2` | a float parameter |
| `"Scene": {"path": "/mnt/projects/promo.blend"}` | a path parameter, which the storage profile can remap |

## Outputs

| Output | Description |
| --- | --- |
| `job_id` | The job's ID. |
| `job_name` | The job's name. |
| `lifecycle_status` | Where the job is in its life cycle, for example `CREATE_COMPLETE`. |
| `task_run_status` | The status of the job's tasks. `SUCCEEDED` when the node waited and succeeded. |
| `task_counts` | How many tasks are in each status, for example `{"SUCCEEDED": 250}`. |
| `started_at` | When the first task started, a UTC time. |
| `ended_at` | When the last task finished, a UTC time. |

The component does not know where the job writes its frames; that is the
template's business. Pass the output folder to the job as a parameter and use
the same value in the next node.

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the farm:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → AWS Deadline Cloud → Set up access**.

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
   `<REGION>`, `<ACCOUNT_ID>`, `<FARM_ID>`, `<QUEUE_ID>`, `<TEMPLATE_BUCKET>`
   and `<TEMPLATE_PREFIX>` with your own. The role can then submit jobs to
   that one queue, read them, search the queue for the job of an earlier
   delivery, and read job templates under one S3 prefix. It cannot cancel or
   change jobs, and it has no access to the scenes or the frames. If you only
   use inline templates, remove the last statement.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

The workers read and write media with the queue's own role, which you set up
in Deadline Cloud. The component passes no role.

## How it runs

- **One job per task.** The job is submitted with a request token made from
  the task id, and its description carries the task id. A second delivery
  searches the queue for that id, finds the job and waits for it instead of
  rendering again. The description of a job this node submits therefore
  replaces the template's own.
- **Waits politely.** The node asks for the job's status every 5 seconds at
  first and every 30 seconds later.
- **Time budget.** Up to 6 hours. If the node is stopped, the job keeps
  rendering on the farm and a retry picks it up. For a longer render, set
  `wait` to `false` and follow the job from a later workflow.
- **A job that does not succeed fails the node:** failed, canceled,
  suspended, or not compatible with any fleet of the queue.
- **SDK version.** Naming a job, limiting its workers and writing the task id
  into its description need a newer AWS SDK than most of the library pins
  (`boto3==1.44.9`), so this component gets an image of its own at install.

## Cost

Deadline Cloud bills the hours its workers run, by instance type, plus any
usage-based licences of the applications the job uses. A job waiting in the
queue costs nothing. Patchbae adds no charge. See the AWS Deadline Cloud
pricing page for your region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "Deadline Cloud would not accept the job: …" | Deadline Cloud's reason follows. Common causes: the template does not validate, a parameter is missing or of the wrong kind, or the farm or queue ID is of another region. |
| "Deadline Cloud could not set the job up (CREATE_FAILED): …" | The template was accepted and then could not be expanded into steps and tasks. The reason names the step. |
| "Render job … failed: 3 of 250 tasks failed" | Open the job in the Deadline Cloud monitor and read the task logs. |
| "Render job … cannot run: no fleet of the queue has workers that meet the job's requirements" | The template asks for an operating system, memory, GPU or licence that no fleet of the queue offers. |
| "The job template s3://… was not found" | Check `template_uri` and that the bucket and prefix are the ones in the component's policy. |
| "Parameter … must be text, a number, or one of …" | Write a path as `{"path": "..."}`. Lists and nested objects are not parameter values. |
| `AccessDenied` on `CreateJob`, `GetJob` or `SearchJobs` | The component's role is missing actions from `iam/policy.json`, or the placeholders were not replaced. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
