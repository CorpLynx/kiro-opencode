# infra/iam-eks: IAM and EKS for topomap's `--iam` and `--eks`

A small Terraform stack, separate from the network lab in `../` (its own
state), that gives [`tools/topomap`](../../tools/topomap) something to find
with `topomap aws --iam --eks`. It spans the management account and two
member accounts of an AWS Organization (built for a Control Tower landing
zone's Audit and Log Archive accounts).

## What it builds

| Account | Resource | What topomap shows | Cost |
|---|---|---|---|
| management | `topomap-lab-fenced-admin` role: AdministratorAccess behind a deny-all permissions boundary | `admin access`, the boundary; the role itself can do nothing | Free |
| management | `topomap-lab-user` in group `topomap-lab-readers`, no password, no access keys | a user with a group and `no MFA` | Free |
| audit | `topomap-lab-auditor` role trusting the management account, ViewOnlyAccess plus an inline policy | a trust link from the management account | Free |
| log archive | `topomap-lab-chained` role trusting the audit role, no permissions | a role-to-role trust link | Free |
| management | EKS cluster `topomap-lab` in its own VPC (two private subnets, no internet gateway, no NAT) | the cluster in its VPC, version, endpoint access | **~$0.10/hour** (control plane) |
| management | node group scaled to 0 (SPOT t3.small), Fargate profile, kube-proxy add-on | node group, Fargate profile and add-on lists | Free: no nodes or pods run |
| management | EKS cluster, node and Fargate roles; OIDC provider; IRSA role | service and federated trust | Free |

EKS also creates its service-linked roles, which topomap hides unless you
pass `--iam-all`.

## Safety

- The member-account roles only trust the management account, directly or
  through the audit role. The management account already administers those
  accounts through `AWSControlTowerExecution`, so no new access path is
  opened.
- The cluster's API endpoint is private only, in a VPC with no internet
  gateway, so nothing outside that VPC can reach it.
- No credentials are created: no passwords, no access keys.
- Account IDs live only in `terraform.tfvars` and state, both gitignored.

## Use

```sh
cd infra/iam-eks
cp terraform.tfvars.example terraform.tfvars   # put in the member account IDs
terraform init
terraform plan
terraform apply                                # the cluster takes ~10-15 minutes
```

Collect it (`terraform output topomap_command` prints this with your values):

```sh
topomap aws --profile default --org-role-name AWSControlTowerExecution \
    --region us-east-1 --iam --eks --skip-network -o topomap-out/iam-eks.json
topomap render topomap-out/iam-eks.json -o topomap-out/iam-eks.svg
```

## Tearing down

The cluster is the only billed part. Remove it and keep the free IAM pieces:

```sh
terraform apply -var create_eks=false
```

Or remove everything:

```sh
terraform destroy
```

Destroy this stack before the network lab in `../` if you ever make the
cluster use that VPC; as written they share nothing.
