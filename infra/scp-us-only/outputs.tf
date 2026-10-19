output "policy_id" {
  value       = aws_organizations_policy.us_only.id
  description = "The SCP; `topomapper aws-org` shows it by name on whatever it is attached to."
}

output "attached_to" {
  value       = local.target_id
  description = "Root, OU or account the policy is attached to."
}

output "allowed_regions" {
  value = var.allowed_regions
}
