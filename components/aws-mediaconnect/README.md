# AWS Elemental MediaConnect

Starts and stops a flow in AWS Elemental MediaConnect, the service that
carries a contribution feed from a venue or a partner into AWS. Use it before
a live event to open the flow and hand the ingest address to whoever sends
the feed, and after the event to close it.

## What it does

| Action | What happens |
| --- | --- |
| `start_flow` | Starts the flow and, by default, waits until it is `ACTIVE`. |
| `stop_flow` | Stops the flow and, by default, waits until it is on `STANDBY`. |

Both actions return the flow's status and its ingest address: the IP address,
port and protocol the contribution encoder sends to.

No media passes through Patchbae. The feed goes from the encoder to
MediaConnect.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `flow_arn` | yes | | ARN of the flow. |
| `action` | yes | | `start_flow` or `stop_flow`. |
| `wait` | no | `true` | Wait until the flow is active or on standby. |

## Outputs

| Output | Description |
| --- | --- |
| `flow_arn` | The flow the action was applied to. |
| `flow_name` | The flow's name. |
| `status` | The flow's status when the node finished, for example `ACTIVE` or `STANDBY`. |
| `ingest_ip` | The IP address to send the feed to. Empty when the source has none, such as an entitlement or a VPC source. |
| `ingest_port` | The port to send the feed to. `0` when the source has none. |
| `ingest_protocol` | The source's protocol, for example `srt-listener`, `rist` or `zixi-push`. |
| `egress_ip` | The IP address the flow's outputs are sent from, for allow lists downstream. |

For a flow with several sources, the ingest address is that of the flow's
current source.

## AWS access

This component runs as its own pod, once per run of the node. The pod has no
AWS keys. It gets temporary credentials by assuming **an IAM role you create
for this component**, through the cluster's OIDC provider (IAM Roles for
Service Accounts). Patchbae never sees or stores credentials; it only stores
the role's ARN.

You create the role once, in the AWS account that holds the flow:

1. **Create an IAM role with this trust policy.** It lets only this
   component's service account assume the role. Patchbae shows the policy
   with your namespace and service account filled in under
   **Components → AWS Elemental MediaConnect → Set up access**.

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
   `<REGION>`, `<ACCOUNT_ID>`, `<FLOW_ID>` and `<FLOW_NAME>` with your own:
   they are the parts of the flow's ARN. The role can then read the flow and
   start and stop it, and nothing else. For more than one flow, list one
   resource per flow.

3. **Paste the role's ARN** into **Set up access** and choose the region.
   The component becomes **Available** and can be added to workflows.

## How it runs

- **Looks before it acts.** `start_flow` on an active flow does nothing, and
  so does `stop_flow` on a flow on standby. A task that is delivered twice
  starts or stops the flow once.
- **Waits politely.** While the flow starts or stops, the node asks for its
  status every 5 seconds at first and every 30 seconds later.
- **Time budget.** 15 minutes. A flow normally starts within a minute.
- **A flow in `ERROR` fails the node** with the errors MediaConnect reports
  for it.

## Cost

MediaConnect bills an active flow by the hour and by the gigabyte it carries.
A flow on standby costs nothing. Patchbae adds no charge. See the AWS
Elemental MediaConnect pricing page for your region.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "The flow was not found" | The ARN is wrong or the component is set up for another region. |
| "Flow … is in ERROR: …" | MediaConnect's errors follow. Fix the flow in the MediaConnect console. |
| "MediaConnect would not start the flow: …" | MediaConnect's reason follows, for example a flow that is being updated. |
| The flow is `ACTIVE` but no feed arrives | The sender's IP address is not in the source's allow list (`WhitelistCidr`), or it sends to another port or protocol than `ingest_port` and `ingest_protocol`. |
| `AccessDenied` on `DescribeFlow`, `StartFlow` or `StopFlow` | The component's role is missing actions from `iam/policy.json`, or the placeholders were not replaced. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy does not match the service account. Copy it again from **Set up access**. |
