variable "project_id" {
  description = "The GCP project everything is created in."
  type        = string
}

variable "region" {
  description = "Region for the VM's subnet and the sign-in proxy."
  type        = string
  default     = "us-east1"
}

variable "zone" {
  description = "Zone for the VM. Must be in var.region."
  type        = string
  default     = "us-east1-b"
}

variable "machine_type" {
  description = "Sized by CPU, not memory: six nodes at 250 payments a second saturated e2-standard-8, whose 8 vCPUs are 4 hyperthreaded cores. T2D gives a full core per vCPU."
  type        = string
  default     = "t2d-standard-8"
}

variable "nodes_per_region" {
  description = "The presets have two nodes per region; the ZONE vs REGION scenario needs three. Nodes are added after the build until every region has this many."
  type        = number
  default     = 3
}

variable "crdb_version" {
  description = "CockroachDB version the cluster runs."
  type        = string
  default     = "v26.4.1"
}

variable "preset" {
  description = "The preset the cluster is built from; see presets/."
  type        = string
  default     = "aws"
}

variable "staff_domain" {
  description = "Everyone with a Google account in this domain gets through the sign-in."
  type        = string
}

variable "banko_sign_in" {
  description = "Sign each person in to Banko as themselves, from the Google identity the sign-in in front has already checked, with the personas a click away. Off leaves Banko's own username page, where anyone can be anyone."
  type        = bool
  default     = true
}

variable "banko_env" {
  description = "Banko's model provider, as the environment variables Banko reads: AI_SERVICE and that provider's credentials. Empty runs Banko with no model, so its pages work and its assistant does not."
  type        = map(string)
  default     = {}
  sensitive   = true
}

variable "crdb_license" {
  description = "CockroachDB license key. Without one the cluster is throttled to 5 open transactions 7 days after it is built, and payments start failing."
  type        = string
  default     = ""
  sensitive   = true
}
