locals {
  name = "${var.project}-${var.environment}"
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

# Public subnets (one per AZ) — the api tier and a NAT gateway (if this
# were carrying real traffic) live here.
resource "aws_subnet" "public" {
  count                   = var.az_count
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public-${count.index}" }
}

# Private subnets — worker/beat hosts and the database (when enabled) live
# here, with no direct route to the internet gateway.
resource "aws_subnet" "private" {
  count             = var.az_count
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 100)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${local.name}-private-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private route table exists (for consistency with a real deployment that
# would attach a NAT gateway here) but deliberately has no default route —
# a NAT gateway is a real per-hour AWS cost with no LocalStack Community
# equivalent worth paying to prove out locally. Documented, not silently
# omitted.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------------------
# Security groups: three tiers, each only accepting from the tier in front
# of it — mirrors the k8s/ NetworkPolicy-shaped thinking (api reachable from
# the internet, worker/beat reachable from nothing external, db reachable
# only from api+worker) even though this repo doesn't define K8s
# NetworkPolicies today.
# ---------------------------------------------------------------------------

resource "aws_security_group" "api" {
  name_prefix = "${local.name}-api-"
  vpc_id      = aws_vpc.main.id
  description = "TalentScope API — inbound 8000 from the internet (would sit behind an ALB in a real deployment), outbound unrestricted."

  ingress {
    description = "API"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-api" }
}

resource "aws_security_group" "worker" {
  name_prefix = "${local.name}-worker-"
  vpc_id      = aws_vpc.main.id
  description = "Celery worker/beat — no inbound from the internet at all; only reachable for operational access (SSH/SSM) from within the VPC."

  ingress {
    description = "Metrics scrape (Prometheus) and operational access, VPC-internal only"
    from_port   = 9808
    to_port     = 9808
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-worker" }
}

resource "aws_security_group" "db" {
  name_prefix = "${local.name}-db-"
  vpc_id      = aws_vpc.main.id
  description = "Postgres — reachable only from the api and worker security groups, never from the internet or a public subnet."

  ingress {
    description     = "Postgres from api"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.api.id, aws_security_group.worker.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-db" }
}
