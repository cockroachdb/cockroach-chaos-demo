output "stage_url" {
  description = "The stage and, at /presenter, the console. Anyone in the staff domain can open and drive both after signing in with Google."
  value       = google_cloud_run_v2_service.stage.uri
}

output "console_url" {
  description = "The DB Console, behind the same sign-in. The presenter's DB Console link opens it."
  value       = google_cloud_run_v2_service.extra["console"].uri
}

output "lb_stats_url" {
  description = "HAProxy's stats page, behind the same sign-in. The presenter's Load balancer link opens it."
  value       = "${google_cloud_run_v2_service.extra["lb"].uri}/stats"
}

output "banko_url" {
  description = "Banko, behind the same sign-in, banking on the demo's cluster."
  value       = google_cloud_run_v2_service.extra["banko"].uri
}

output "ssh" {
  description = "A shell on the VM, through IAP."
  value       = "gcloud compute ssh ${google_compute_instance.this.name} --zone ${var.zone} --project ${var.project_id} --tunnel-through-iap"
}
