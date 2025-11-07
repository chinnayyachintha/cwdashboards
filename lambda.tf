# ---------------------------------
# Archive the Lambda Function Code
# ---------------------------------
data "archive_file" "cw_dashboard_emailer_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda_code/lambda_function.py"
  output_path = "${path.module}/lambda_code/lambda_function.zip"
}

# -----------------------------
# Lambda Function Configuration
# -----------------------------
resource "aws_lambda_function" "cw_dashboard_emailer" {
  filename         = data.archive_file.cw_dashboard_emailer_zip.output_path
  function_name    = var.lambda_function_name
  role             = aws_iam_role.lambda_exec_role.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.11"
  timeout          = 900
  memory_size      = 512
  source_code_hash = data.archive_file.cw_dashboard_emailer_zip.output_base64sha256

  environment {
    variables = {
      S3_BUCKET            = aws_s3_bucket.securityhub_reports.bucket
      SES_SENDER_EMAIL     = var.ses_sender_email
      SES_RECIPIENT_EMAILS = var.ses_recipient_emails
    }
  }

  tags = {
    Environment = var.env_name
    Purpose     = "CloudWatch Dashboard Emailer"
  }
}