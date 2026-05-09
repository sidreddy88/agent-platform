# ACM certificate for app.remediatelabs.io. DNS-validated.
#
# Cloudflare hosts the DNS for remediatelabs.io. Terraform creates the
# cert resource and surfaces the validation CNAME via the
# `acm_validation_record` output — you copy that record into Cloudflare
# (DNS-only mode, NOT proxied). ACM detects the record and the cert
# moves to ISSUED in 5–15 minutes; only then does
# aws_acm_certificate_validation succeed and the HTTPS listener can
# come up.

resource "aws_acm_certificate" "app" {
  domain_name       = var.hostname
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# Wait for the cert to be validated before allowing the HTTPS listener
# to attach. The validation_record_fqdns input is the list of FQDNs
# that ACM reads from DNS — the resource blocks on them resolving.
resource "aws_acm_certificate_validation" "app" {
  certificate_arn         = aws_acm_certificate.app.arn
  validation_record_fqdns = [for o in aws_acm_certificate.app.domain_validation_options : o.resource_record_name]
}
