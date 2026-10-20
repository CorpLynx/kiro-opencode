# A cheap, throwaway AWS estate that `topomapper account collect` gathers.
#
# Everything here is free or near-free: VPC, subnets, route tables and the
# internet gateway cost nothing; the S3 gateway VPC endpoint costs nothing;
# the single EC2 instance is t3.micro (free-tier eligible for 12 months,
# roughly $8/month afterward) or t4g.nano (roughly $3/month). NAT gateways,
# transit gateways, VPN and Direct Connect are deliberately left out - they
# cost money and topomapper can draw them if you ever add them.
#
# All addresses are RFC 1918. No credentials, account IDs or real network
# data are in this file. Review the plan before applying:
#
#   terraform init
#   cp terraform.tfvars.example terraform.tfvars   # edit as needed
#   terraform plan
#   terraform apply
#   terraform destroy                              # when you're done

provider "aws" {
  region = var.region
}

# Two availability zones in this region. Read-only; free.
data "aws_availability_zones" "available" {
  state = "available"
}

# Amazon Linux 2023 (x86_64) AMI for this region. Read-only; free.
data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name        = "topomap-lab-vpc"
    topomap_lab = "true"
  }
}

# Public subnet: it gets a route to the internet gateway, so topomapper marks
# it public.
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = {
    Name        = "topomap-lab-public"
    topomap_lab = "true"
  }
}

# Private subnet: no route to an internet gateway, so topomapper marks it
# private.
resource "aws_subnet" "private" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.2.0/24"
  availability_zone = data.aws_availability_zones.available.names[1]

  tags = {
    Name        = "topomap-lab-private"
    topomap_lab = "true"
  }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name        = "topomap-lab-igw"
    topomap_lab = "true"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }

  tags = {
    Name        = "topomap-lab-public-rt"
    topomap_lab = "true"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# The private route table has only the implicit local route, so instances in
# the private subnet cannot reach the internet - exactly what topomapper's
# public/private detection looks for.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name        = "topomap-lab-private-rt"
    topomap_lab = "true"
  }
}

resource "aws_route_table_association" "private" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}

# SSH from a documentation range only. Security groups themselves are not
# collected by topomap, but the instance inside them is.
resource "aws_security_group" "ssh" {
  name        = "topomap-lab-ssh"
  description = "SSH (22) from the documentation range 203.0.113.0/24 only"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["203.0.113.0/24"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "topomap-lab-ssh"
    topomap_lab = "true"
  }
}

resource "aws_instance" "web" {
  count = var.create_instance ? 1 : 0

  ami                         = data.aws_ssm_parameter.ami.value
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.ssh.id]
  associate_public_ip_address = true

  # No key pair: this instance exists to appear in a topomapper graph, not to
  # log in to. If you need to connect, use AWS Systems Manager Session
  # Manager with an instance role, or add a key pair.

  tags = {
    Name        = "topomap-lab-web"
    topomap_lab = "true"
  }
}

# Gateway VPC endpoint for S3. Free, and collected by topomapper as
# aws:vpce:vpce-... when you add the endpoints word.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]

  tags = {
    Name        = "topomap-lab-s3-endpoint"
    topomap_lab = "true"
  }
}
