# infra: a cheap AWS estate for topomapper

This folder holds a tiny Terraform estate (VPC, public and private subnets,
an internet gateway, one small EC2 instance and a free S3 gateway VPC
endpoint) built on purpose to be gathered by [`tools/topomapper`](../tools/topomapper)'s
`topomapper account collect`.

## What topomapper catches here

| Resource | Terraform | Collected by topomapper | Cost |
|---|---|---|---|
| VPC | `aws_vpc.main` | Always, as `aws:vpc:vpc-...` | Free |
| Public subnet | `aws_subnet.public` | Always, marked public (route to IGW) | Free |
| Private subnet | `aws_subnet.private` | Always, marked private | Free |
| Internet gateway | `aws_internet_gateway.igw` | Always | Free |
| EC2 instance | `aws_instance.web` | With the `instances` word | t3.micro: free tier for 12 months, ~$8/month after; t4g.nano: ~$3/month |
| S3 VPC endpoint (gateway) | `aws_vpc_endpoint.s3` | With the `endpoints` word | Free |

NAT gateways, transit gateways, VPN and Direct Connect are deliberately not
included: they cost money. Add them later if you want topomapper to draw TGW
attachments, VPN tunnels or DX VIFs.

## How to use

```sh
cd infra
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars if you want another region or instance type
terraform init
terraform plan
terraform apply
```

Then collect it with topomapper:

```sh
topomapper account collect network instances endpoints --profile default \
    --region us-east-1 aws.json aws.drawio
```

When you're done:

```sh
terraform destroy
```

## Related stacks

- [`iam-eks/`](iam-eks): IAM roles and users across accounts, and a minimal EKS cluster.
- [`scp-us-only/`](scp-us-only): a service control policy that keeps member accounts in US regions. Both this stack and `iam-eks/` only accept US regions.

## Cost and safety

- Destroy promptly. The only billable piece is the EC2 instance.
- Nothing here is secret: there are no credentials, account IDs or real
  network addresses. All addresses are RFC 1918 (`10.0.0.0/16` VPC,
  `10.0.1.0/24` and `10.0.2.0/24` subnets; SSH allowed from the documentation
  range `203.0.113.0/24`).
- State and `terraform.tfvars` are gitignored. Do not commit them: state can
  contain your resource IDs and addresses.

## Why this is not in CI

This directory creates real AWS resources and needs real credentials, so it
is not part of the Python CI matrix (which installs each project with
`pip install .` and runs `unittest`). It is reviewed by hand instead.
