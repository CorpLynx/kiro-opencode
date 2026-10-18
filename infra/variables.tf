variable "region" {
  description = "AWS region to build the lab in. US regions only (see scp-us-only/)."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = contains(["us-east-1", "us-east-2", "us-west-1", "us-west-2"], var.region)
    error_message = "Only US commercial regions are allowed: us-east-1, us-east-2, us-west-1 or us-west-2."
  }
}

variable "instance_type" {
  description = "EC2 instance type. t3.micro is free-tier eligible for the first 12 months; t4g.nano is about $3/month outside free tier."
  type        = string
  default     = "t3.micro"
}

variable "create_instance" {
  description = "Set false to build a $0 VPC-only estate (VPC, subnets, IGW, S3 endpoint)."
  type        = bool
  default     = true
}
