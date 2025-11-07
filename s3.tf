# ------------------------------
# Step 1: S3 Bucket for CSV Reports
# ------------------------------

resource "aws_s3_bucket" "securityhub_reports" {
  bucket        = "s3-dashboard-${var.env_name}" # Must be unique globally
  force_destroy = true

  tags = {
    Environment = var.env_name
  }
}

# Enable Versioning
resource "aws_s3_bucket_versioning" "versioning" {
  bucket = aws_s3_bucket.securityhub_reports.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Enable Server-Side Encryption (SSE-S3)
resource "aws_s3_bucket_server_side_encryption_configuration" "encryption" {
  bucket = aws_s3_bucket.securityhub_reports.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Optional Lifecycle Policy (delete CSVs older than 30 days)
resource "aws_s3_bucket_lifecycle_configuration" "lifecycle" {
  bucket = aws_s3_bucket.securityhub_reports.id

  rule {
    id     = "expire-old-csvs"
    status = "Enabled"

    filter {
      prefix = "reports/"
    }

    expiration {
      days = 30
    }
  }
}

 