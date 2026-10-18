# IAM roles and users for topomap's `collect aws --iam`. All of it is free.
#
# What each piece exercises in topomap:
#
#   management  topomap-lab-fenced-admin   admin_access, permissions_boundary
#                                          (AdministratorAccess fenced by a
#                                          boundary that denies everything, so
#                                          the role can do nothing at all)
#   management  topomap-lab-user           an IAM user with a group, no MFA and
#                                          no access keys or password
#   audit       topomap-lab-auditor        cross-account trust: a link from the
#                                          management account, plus attached
#                                          and inline policies
#   log archive topomap-lab-chained        role-to-role trust: a link from the
#                                          audit role; it has no permissions
#
# The member-account roles only trust the management account (directly or
# through the audit role), which already administers those accounts through
# AWSControlTowerExecution, so no new access path is opened. EKS's own roles
# and the IRSA role are in eks.tf.

provider "aws" {
  region  = var.region
  profile = var.profile

  default_tags {
    tags = { topomap_lab = "true" }
  }
}

provider "aws" {
  alias   = "audit"
  region  = var.region
  profile = var.profile

  assume_role {
    role_arn     = "arn:aws:iam::${var.audit_account_id}:role/${var.member_role_name}"
    session_name = "topomap-lab"
  }

  default_tags {
    tags = { topomap_lab = "true" }
  }
}

provider "aws" {
  alias   = "log_archive"
  region  = var.region
  profile = var.profile

  assume_role {
    role_arn     = "arn:aws:iam::${var.log_archive_account_id}:role/${var.member_role_name}"
    session_name = "topomap-lab"
  }

  default_tags {
    tags = { topomap_lab = "true" }
  }
}

data "aws_caller_identity" "management" {}
data "aws_partition" "current" {}

locals {
  partition       = data.aws_partition.current.partition
  management_root = "arn:${local.partition}:iam::${data.aws_caller_identity.management.account_id}:root"
  aws_policy      = "arn:${local.partition}:iam::aws:policy"

  whoami_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:GetCallerIdentity", Resource = "*" }]
  })
}

# -- management account ------------------------------------------------------------

resource "aws_iam_policy" "deny_all" {
  name        = "topomap-lab-deny-all-boundary"
  description = "Permissions boundary that allows nothing, so a role behind it is inert."
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Sid = "DenyEverything", Effect = "Deny", Action = "*", Resource = "*" }]
  })
}

resource "aws_iam_role" "fenced_admin" {
  name                 = "topomap-lab-fenced-admin"
  description          = "topomap test: AdministratorAccess behind a deny-all boundary (can do nothing)."
  permissions_boundary = aws_iam_policy.deny_all.arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = local.management_root }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "fenced_admin" {
  role       = aws_iam_role.fenced_admin.name
  policy_arn = "${local.aws_policy}/AdministratorAccess"
}

resource "aws_iam_group" "readers" {
  name = "topomap-lab-readers"
}

resource "aws_iam_group_policy_attachment" "readers" {
  group      = aws_iam_group.readers.name
  policy_arn = "${local.aws_policy}/job-function/ViewOnlyAccess"
}

# No password and no access keys: the user can't sign in or call AWS. It
# exists so topomap has a user with a group and no MFA to report. An access
# key is left out on purpose, because Terraform would keep its secret in
# state; create and deactivate one by hand to see key age in topomap.
resource "aws_iam_user" "lab" {
  name = "topomap-lab-user"
}

resource "aws_iam_user_group_membership" "lab" {
  user   = aws_iam_user.lab.name
  groups = [aws_iam_group.readers.name]
}

# -- audit account -----------------------------------------------------------------

resource "aws_iam_role" "auditor" {
  provider    = aws.audit
  name        = "topomap-lab-auditor"
  description = "topomap test: cross-account role trusting the management account."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = local.management_root }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "auditor" {
  provider   = aws.audit
  role       = aws_iam_role.auditor.name
  policy_arn = "${local.aws_policy}/job-function/ViewOnlyAccess"
}

resource "aws_iam_role_policy" "auditor_whoami" {
  provider = aws.audit
  name     = "topomap-lab-whoami"
  role     = aws_iam_role.auditor.id
  policy   = local.whoami_policy
}

# -- log archive account -----------------------------------------------------------

resource "aws_iam_role" "chained" {
  provider    = aws.log_archive
  name        = "topomap-lab-chained"
  description = "topomap test: trusts the audit account's topomap-lab-auditor role; no permissions."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.auditor.arn }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "chained_whoami" {
  provider = aws.log_archive
  name     = "topomap-lab-whoami"
  role     = aws_iam_role.chained.id
  policy   = local.whoami_policy
}
