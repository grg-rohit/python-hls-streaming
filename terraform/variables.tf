variable "region" {
  type = string
  default = "us-east-1"
}
variable "instance_type" {
  type = string
  default = "m6i.xlarge"
}
variable "disk_gb" {
  type = number
  default = 100
}
variable "ssh_public_key_path" {
  type = string
  default = "~/.ssh/id_ed25519.pub"
}
variable "ssh_cidr" {
  type = string
  description = "Trusted administrator IPv4 address in CIDR notation, normally /32."
  validation {
    condition = can(cidrnetmask(var.ssh_cidr)) && var.ssh_cidr != "0.0.0.0/0"
    error_message = "Use a restricted IPv4 CIDR, not 0.0.0.0/0."
  }
}
