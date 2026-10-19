# A service control policy that keeps member accounts in US regions.
#
# It denies every action requested in a region outside the allow list,
# except for the global services that have no regional endpoint of their own
# (IAM, Organizations, STS, Route 53, billing and so on). Without that
# exemption, nothing could sign in or call the control plane. The list below
# follows the one AWS Control Tower publishes for its region deny guardrail.
#
# What it does and does not do:
#
#   - It blocks new API calls in other regions, in every account under the
#     target. It does not delete anything that is already there, and the
#     default VPCs AWS creates in every region stay until someone removes them.
#   - It never applies to the management account. SCPs don't, by design, so
#     that account can always repair a bad policy. Keep that in mind: this is
#     how you stay in control, and also why the management account still
#     shows all 17 regions to topomapper.
#   - Control Tower governs us-east-1 and us-west-2 here, both allowed.
#
# Attached to the organization root by default, so it covers every current
# and future account. Detach or destroy to undo it at once.
#
#   terraform init && terraform plan && terraform apply
#
# Nothing here is secret: the policy has no account IDs.

provider "aws" {
  region  = var.home_region
  profile = var.profile

  default_tags {
    tags = { topomap_lab = "true" }
  }
}

data "aws_organizations_organization" "this" {}

locals {
  target_id = coalesce(var.target_id, data.aws_organizations_organization.this.roots[0].id)

  # Services with a global endpoint, or whose control plane lives in one region.
  global_services = [
    "a4b:*",
    "access-analyzer:*",
    "account:*",
    "acm:*",
    "aws-marketplace-management:*",
    "aws-marketplace:*",
    "aws-portal:*",
    "billing:*",
    "budgets:*",
    "ce:*",
    "chime:*",
    "cloudfront:*",
    "config:*",
    "cur:*",
    "directconnect:*",
    "ec2:DescribeRegions",
    "ec2:DescribeTransitGateways",
    "ec2:DescribeVpnGateways",
    "fms:*",
    "globalaccelerator:*",
    "health:*",
    "iam:*",
    "importexport:*",
    "mobileanalytics:*",
    "networkmanager:*",
    "organizations:*",
    "pricing:*",
    "route53-recovery-cluster:*",
    "route53-recovery-control-config:*",
    "route53-recovery-readiness:*",
    "route53:*",
    "route53domains:*",
    "s3:CreateMultiRegionAccessPoint",
    "s3:GetAccountPublicAccessBlock",
    "s3:GetBucketLocation",
    "s3:GetBucketPolicyStatus",
    "s3:GetBucketPublicAccessBlock",
    "s3:ListAllMyBuckets",
    "s3:PutAccountPublicAccessBlock",
    "shield:*",
    "sts:*",
    "support:*",
    "trustedadvisor:*",
    "waf-regional:*",
    "waf:*",
    "wafv2:*",
    "wellarchitected:*",
  ]
}

resource "aws_organizations_policy" "us_only" {
  name        = "topomap-lab-us-regions-only"
  description = "Deny requests to regions outside ${join(", ", var.allowed_regions)}, except global services."
  type        = "SERVICE_CONTROL_POLICY"

  content = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyOutsideUsRegions"
      Effect    = "Deny"
      NotAction = local.global_services
      Resource  = "*"
      Condition = {
        StringNotEquals = {
          "aws:RequestedRegion" = var.allowed_regions
        }
      }
    }]
  })
}

resource "aws_organizations_policy_attachment" "us_only" {
  policy_id = aws_organizations_policy.us_only.id
  target_id = local.target_id
}
