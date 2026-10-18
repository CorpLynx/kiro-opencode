variable "region" {
  description = "Region for the EKS cluster and its VPC. IAM is global. US regions only (see ../scp-us-only/)."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = contains(["us-east-1", "us-east-2", "us-west-1", "us-west-2"], var.region)
    error_message = "Only US commercial regions are allowed: us-east-1, us-east-2, us-west-1 or us-west-2."
  }
}

variable "profile" {
  description = "AWS CLI profile with credentials for the management account."
  type        = string
  default     = "default"
}

variable "audit_account_id" {
  description = "Member account that gets a cross-account role trusting the management account."
  type        = string
}

variable "log_archive_account_id" {
  description = "Member account that gets a role trusting the role in the audit account."
  type        = string
}

variable "member_role_name" {
  description = "Role the management account assumes in member accounts to create the IAM roles there."
  type        = string
  default     = "AWSControlTowerExecution"
}

variable "create_eks" {
  description = "Build the EKS cluster. It is the only part that costs money (about $0.10/hour); set false to tear it down and keep the free IAM resources."
  type        = bool
  default     = true
}

variable "eks_version" {
  description = "Kubernetes version. null uses the version EKS currently defaults to."
  type        = string
  default     = null
}
