terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
provider "aws" {
  region = var.region
}
data "aws_ami" "ubuntu" {
  most_recent = true
  owners = ["099720109477"]
  filter {
    name = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name = "architecture"
    values = ["x86_64"]
  }
}
resource "aws_vpc" "hls" {
  cidr_block = "10.42.0.0/16"
  enable_dns_support = true
  enable_dns_hostnames = true
  tags = { Name = "hls" }
}
resource "aws_subnet" "hls" {
  vpc_id = aws_vpc.hls.id
  cidr_block = "10.42.1.0/24"
  map_public_ip_on_launch = true
}
resource "aws_internet_gateway" "hls" {
  vpc_id = aws_vpc.hls.id
}
resource "aws_route_table" "hls" {
  vpc_id = aws_vpc.hls.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.hls.id
  }
}
resource "aws_route_table_association" "hls" {
  subnet_id = aws_subnet.hls.id
  route_table_id = aws_route_table.hls.id
}
resource "aws_security_group" "hls" {
  name_prefix = "hls-"
  vpc_id = aws_vpc.hls.id
  ingress {
    description = "Administrator SSH"
    from_port = 22
    to_port = 22
    protocol = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }
  egress {
    from_port = 0
    to_port = 0
    protocol = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_key_pair" "hls" {
  key_name_prefix = "hls-"
  public_key = file(pathexpand(var.ssh_public_key_path))
}
resource "aws_instance" "hls" {
  ami = data.aws_ami.ubuntu.id
  instance_type = var.instance_type
  subnet_id = aws_subnet.hls.id
  vpc_security_group_ids = [aws_security_group.hls.id]
  key_name = aws_key_pair.hls.key_name
  user_data = file("${path.module}/cloud-init.yaml")
  metadata_options {
    http_tokens = "required"
  }
  root_block_device {
    volume_type = "gp3"
    volume_size = var.disk_gb
    encrypted = true
  }
  tags = { Name = "hls-compose" }
}
output "public_ip" {
  value = aws_instance.hls.public_ip
}
output "ssh_command" {
  value = "ssh ubuntu@${aws_instance.hls.public_ip}"
}
