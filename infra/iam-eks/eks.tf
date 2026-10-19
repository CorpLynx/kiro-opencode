# The cheapest EKS cluster that still has everything topomapper's
# `topomapper aws --eks` reads. Only the control plane is billed (about
# $0.10/hour, ~$2.40/day); set create_eks = false to tear it down.
#
#   VPC         two private subnets, no internet gateway and no NAT (free)
#   cluster     private API endpoint only (EKS rejects documentation ranges
#               as public CIDRs, so the public endpoint is simply off)
#   node group  SPOT t3.small, scaled to 0 nodes: no EC2 is ever started
#   Fargate     a profile for one namespace; no pods, so no charge
#   add-on      kube-proxy (managed), so the add-on list isn't empty
#   IRSA        the cluster's OIDC provider and a role trusting it, which
#               topomapper shows as a federated trust
#
# Control-plane logging and secrets encryption are left off: both would add
# CloudWatch or KMS charges. Scaling the node group up won't work as-is,
# because the subnets have no route to the internet for nodes to join.

locals {
  eks_count = var.create_eks ? 1 : 0
}

# use1-az3 can't host EKS control planes.
data "aws_availability_zones" "eks" {
  state            = "available"
  exclude_zone_ids = ["use1-az3"]
}

resource "aws_vpc" "eks" {
  count                = local.eks_count
  cidr_block           = "10.40.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = "topomap-lab-eks-vpc" }
}

resource "aws_subnet" "eks" {
  count             = var.create_eks ? 2 : 0
  vpc_id            = aws_vpc.eks[0].id
  cidr_block        = "10.40.${count.index + 1}.0/24"
  availability_zone = data.aws_availability_zones.eks.names[count.index]
  tags              = { Name = "topomap-lab-eks-private-${count.index + 1}" }
}

# -- roles EKS needs -----------------------------------------------------------------

resource "aws_iam_role" "eks_cluster" {
  name        = "topomap-lab-eks-cluster"
  description = "topomap test: EKS cluster service role."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_cluster" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "${local.aws_policy}/AmazonEKSClusterPolicy"
}

resource "aws_iam_role" "eks_node" {
  name        = "topomap-lab-eks-node"
  description = "topomap test: EKS node group instance role."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_node" {
  for_each = toset(["AmazonEKSWorkerNodePolicy", "AmazonEKS_CNI_Policy",
  "AmazonEC2ContainerRegistryReadOnly"])
  role       = aws_iam_role.eks_node.name
  policy_arn = "${local.aws_policy}/${each.value}"
}

resource "aws_iam_role" "eks_fargate" {
  name        = "topomap-lab-eks-fargate"
  description = "topomap test: EKS Fargate pod execution role."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks-fargate-pods.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_fargate" {
  role       = aws_iam_role.eks_fargate.name
  policy_arn = "${local.aws_policy}/AmazonEKSFargatePodExecutionRolePolicy"
}

# -- the cluster ---------------------------------------------------------------------

resource "aws_eks_cluster" "lab" {
  count    = local.eks_count
  name     = "topomap-lab"
  role_arn = aws_iam_role.eks_cluster.arn
  version  = var.eks_version

  vpc_config {
    subnet_ids              = aws_subnet.eks[*].id
    endpoint_private_access = true
    endpoint_public_access  = false
  }

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  # Stay on standard-support pricing; extended support costs six times as much.
  upgrade_policy {
    support_type = "STANDARD"
  }

  depends_on = [aws_iam_role_policy_attachment.eks_cluster]
}

resource "aws_eks_node_group" "zero" {
  count           = local.eks_count
  cluster_name    = aws_eks_cluster.lab[0].name
  node_group_name = "topomap-lab-zero"
  node_role_arn   = aws_iam_role.eks_node.arn
  subnet_ids      = aws_subnet.eks[*].id
  capacity_type   = "SPOT"
  instance_types  = ["t3.small"]
  ami_type        = "AL2023_x86_64_STANDARD"

  scaling_config {
    desired_size = 0
    min_size     = 0
    max_size     = 1
  }

  depends_on = [aws_iam_role_policy_attachment.eks_node]
}

resource "aws_eks_fargate_profile" "lab" {
  count                  = local.eks_count
  cluster_name           = aws_eks_cluster.lab[0].name
  fargate_profile_name   = "topomap-lab"
  pod_execution_role_arn = aws_iam_role.eks_fargate.arn
  subnet_ids             = aws_subnet.eks[*].id

  selector {
    namespace = "topomap-lab"
  }

  depends_on = [aws_iam_role_policy_attachment.eks_fargate]
}

resource "aws_eks_addon" "kube_proxy" {
  count                       = local.eks_count
  cluster_name                = aws_eks_cluster.lab[0].name
  addon_name                  = "kube-proxy"
  resolve_conflicts_on_create = "OVERWRITE"
}

# -- IRSA ------------------------------------------------------------------------------

data "tls_certificate" "oidc" {
  count = local.eks_count
  url   = aws_eks_cluster.lab[0].identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  count           = local.eks_count
  url             = aws_eks_cluster.lab[0].identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.oidc[0].certificates[0].sha1_fingerprint]
}

resource "aws_iam_role" "irsa" {
  count       = local.eks_count
  name        = "topomap-lab-irsa"
  description = "topomap test: IRSA role for one service account; no permissions."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.eks[0].arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${replace(aws_eks_cluster.lab[0].identity[0].oidc[0].issuer, "https://", "")}:sub" = "system:serviceaccount:topomap-lab:reader"
        }
      }
    }]
  })
}
