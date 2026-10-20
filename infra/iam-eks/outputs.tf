output "eks_cluster" {
  value       = var.create_eks ? aws_eks_cluster.lab[0].name : null
  description = "EKS cluster topomapper collects with the eks part."
}

output "iam_roles" {
  value = {
    management  = [aws_iam_role.fenced_admin.name, aws_iam_role.eks_cluster.name, aws_iam_role.eks_node.name, aws_iam_role.eks_fargate.name]
    audit       = [aws_iam_role.auditor.name]
    log_archive = [aws_iam_role.chained.name]
  }
  description = "Custom roles topomapper collects with the iam part."
}

output "topomap_command" {
  value       = "topomapper account collect iam eks --profile ${var.profile} --org-role-name ${var.member_role_name} --region ${var.region} topomapper-out/iam-eks.json"
  description = "Collects the management account directly and every member account through the member role."
}
