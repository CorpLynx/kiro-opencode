output "vpc_id" {
  value       = aws_vpc.main.id
  description = "VPC collected by topomap as aws:vpc:vpc-..."
}

output "public_subnet_id" {
  value       = aws_subnet.public.id
  description = "Public subnet (has a route to the internet gateway)."
}

output "private_subnet_id" {
  value       = aws_subnet.private.id
  description = "Private subnet (no route to an internet gateway)."
}

output "internet_gateway_id" {
  value       = aws_internet_gateway.igw.id
  description = "Internet gateway attached to the VPC."
}

output "instance_id" {
  value       = aws_instance.web[0].id
  description = "EC2 instance; collected by topomap with --include instances."
  sensitive   = false
}

output "instance_public_ip" {
  value       = aws_instance.web[0].public_ip
  description = "Public IP of the lab instance."
  sensitive   = false
}

output "vpc_endpoint_id" {
  value       = aws_vpc_endpoint.s3.id
  description = "S3 gateway VPC endpoint; collected by topomap with --include endpoints."
}
