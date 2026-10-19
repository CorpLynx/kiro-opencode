# infra/scp-us-only: keep member accounts in US regions

One service control policy, attached to the organization root, that denies
every request to a region outside `us-east-1`, `us-east-2`, `us-west-1` and
`us-west-2`. Global services (IAM, Organizations, STS, Route 53, billing and
the rest of the list in `main.tf`) are exempt, because they have no regional
endpoint and the accounts could not work without them.

## What it does and doesn't do

- It blocks API calls in other regions in every member account, current and
  future. It does not delete anything: the default VPCs AWS creates in every
  region stay until someone removes them.
- SCPs never apply to the management account, so it can always repair a bad
  policy. That account can still use every region.
- Control Tower governs `us-east-1` and `us-west-2` in this organization;
  both are allowed. Check your own governed regions before adding or
  removing any from `allowed_regions`.
- GovCloud is a separate partition with its own organization. This policy
  can't govern it; allowed regions must be US commercial regions, and the
  variable rejects anything else.

## Use

```sh
cd infra/scp-us-only
terraform init
terraform plan
terraform apply          # takes effect within about a minute
```

Undo it at once with `terraform destroy`.

## Seeing it in topomapper

`topomapper aws-org` shows the policy by name on the root. `topomapper aws` marks
every region it blocks, labelled `SCP blocks EC2, EKS`, and prints one
warning per blocked service and region. `filter --hide-empty` leaves those
regions off a diagram.
