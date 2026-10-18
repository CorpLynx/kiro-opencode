terraform {
  required_version = ">= 1.3"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}

variable "profile" {
  description = "AWS CLI profile with credentials for the organization's management account."
  type        = string
  default     = "default"
}

variable "home_region" {
  description = "Region the Organizations API is called in."
  type        = string
  default     = "us-east-1"
}

variable "allowed_regions" {
  description = "Regions member accounts may use. US commercial regions only."
  type        = list(string)
  default     = ["us-east-1", "us-east-2", "us-west-1", "us-west-2"]

  validation {
    condition     = length(var.allowed_regions) > 0 && alltrue([for r in var.allowed_regions : can(regex("^us-(east|west)-[0-9]+$", r))])
    error_message = "allowed_regions must be US commercial regions such as us-east-1 (GovCloud is a separate partition and this policy can't govern it)."
  }
}

variable "target_id" {
  description = "Root, OU or account ID to attach the policy to. null attaches it to the organization root."
  type        = string
  default     = null
}
