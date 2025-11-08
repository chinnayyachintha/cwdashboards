# ---------------------------------
# Archive the Lambda Function Code
# ---------------------------------
data "archive_file" "cw_dashboard_excel_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda_code/lambda_function.py"
  output_path = "${path.module}/lambda_code/lambda_function.zip"
}

# -----------------------------
# Lambda Function Configuration
# -----------------------------
resource "aws_lambda_function" "cw_dashboard_excel" {
  filename         = data.archive_file.cw_dashboard_excel_zip.output_path
  function_name    = var.lambda_function_name
  role             = aws_iam_role.lambda_exec_role.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.11"
  timeout          = 900
  memory_size      = 512
  source_code_hash = data.archive_file.cw_dashboard_excel_zip.output_base64sha256

  # ---- Attach the XlsxWriter Layer (must exist or defined in TF) ----
  layers = [
    aws_lambda_layer_version.xlsxwriter_layer.arn
  ]

  environment {
    variables = {
      # ---- Mandatory ----
      S3_BUCKET            = aws_s3_bucket.securityhub_reports.bucket
      SES_SENDER_EMAIL     = var.ses_sender_email
      SES_RECIPIENT_EMAILS = var.ses_recipient_emails

      # ---- Optional / Tunable ----
      LOOKBACK_ISO       = "-PT24H" # 24h lookback
      PERIOD_SECONDS     = "300"    # 5-min period
      MAX_METRICS_PER_NS = "100"    # limit per namespace
      IMG_SCALE          = "0.35"   # image scaling in Excel
      ATTACH_EXCEL       = "true"   # attach Excel in email
      MAX_EMAIL_MB       = "7"      # cap for email attachments
      NAMESPACES         = ""       # empty = auto-discover all
    }
  }

  tags = {
    Environment = var.env_name
    Purpose     = "CloudWatch Metrics Excel Dashboard Emailer"
  }

  depends_on = [
    aws_lambda_layer_version.xlsxwriter_layer
  ]
}

# ---------------------------------
# Lambda Layer for XlsxWriter (3.2.0)
# ---------------------------------
resource "null_resource" "build_xlsxwriter_layer" {
  provisioner "local-exec" {
    command = <<EOF
      mkdir -p ${path.module}/lambda_layer/python
      pip3 install XlsxWriter==3.2.0 -t ${path.module}/lambda_layer/python/
      cd ${path.module}/lambda_layer
      zip -r xlsxwriter_layer.zip python
    EOF
  }

  triggers = { always_run = timestamp() }
}

resource "aws_lambda_layer_version" "xlsxwriter_layer" {
  filename            = "${path.module}/lambda_layer/xlsxwriter_layer.zip"
  layer_name          = "xlsxwriter-layer-3-2-0"
  description         = "XlsxWriter 3.2.0 for CloudWatch Excel Dashboards"
  compatible_runtimes = ["python3.11"]
  source_code_hash    = filebase64sha256("${path.module}/lambda_layer/xlsxwriter_layer.zip")
  depends_on          = [null_resource.build_xlsxwriter_layer]
}
