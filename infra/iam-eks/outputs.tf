output "eks_cluster" {
  value       = var.create_eks ? aws_eks_cluster.lab[0].name : null
  description = "EKS cluster topomap collects with --eks."
}

output "iam_roles" {
  value = {
    management  = [aws_iam_role.fenced_admin.name, aws_iam_role.eks_cluster.name, aws_iam_role.eks_node.name, aws_iam_role.eks_fargate.name]
    audit       = [aws_iam_role.auditor.name]
    log_archive = [aws_iam_role.chained.name]
  }
  description = "Custom roles topomap collects with --iam."
}

output "topomap_command" {
  value       = "topomap collect aws --profile ${var.profile} --org-role-name ${var.member_role_name} --region ${var.region} --iam --eks --skip-network -o topomap-out/iam-eks.json"
  description = "Collects the management account directly and every member account through the member role."
}
