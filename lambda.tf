# ===========================================================
# Lambda: CloudWatch Metrics → Excel Dashboards → S3 + Email
# ===========================================================

# ---------------------------------
# Archive the Lambda Function Code
# ---------------------------------
data "archive_file" "cw_dashboard_emailer_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda_code/lambda_function.py"
  output_path = "${path.module}/lambda_code/lambda_function.zip"
}

# ------------------------------------------------------
# Create Local Lambda Layers (matplotlib, numpy, pillow, xlsxwriter)
# ------------------------------------------------------
# Each directory under lambda_layers/ should have its own python/ folder.
# Example structure:
#   lambda_layers/
#     matplotlib_layer/python/...
#     numpy_layer/python/...
#     pillow_layer/python/...
#     xlsxwriter_layer/python/...

locals {
  lambda_layers = {
    "matplotlib_layer" = "matplotlib"
    "numpy_layer"      = "numpy"
    "pillow_layer"     = "pillow"
    "xlsxwriter_layer" = "xlsxwriter"
  }
}

# Archive each local layer
data "archive_file" "lambda_layers" {
  for_each    = local.lambda_layers
  type        = "zip"
  source_dir  = "${path.module}/lambda_layers/${each.key}"
  output_path = "${path.module}/lambda_layers/${each.key}.zip"
}

# Create each layer version in AWS Lambda
resource "aws_lambda_layer_version" "lambda_layers" {
  for_each = local.lambda_layers

  filename            = data.archive_file.lambda_layers[each.key].output_path
  layer_name          = "${each.value}_layer"
  compatible_runtimes = ["python3.12", "python3.13"]
  description         = "Lambda layer for ${each.value}"
}

# ---------------------------------
# Lambda Function Configuration
# ---------------------------------
resource "aws_lambda_function" "cw_dashboard_emailer" {
  filename         = data.archive_file.cw_dashboard_emailer_zip.output_path
  function_name    = var.lambda_function_name
  role             = aws_iam_role.lambda_exec_role.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.13"
  timeout          = 900
  memory_size      = 1024
  source_code_hash = data.archive_file.cw_dashboard_emailer_zip.output_base64sha256

  # Attach all 4 custom layers + AWS public SDK layer (pandas)
  layers = concat(
    [
      aws_lambda_layer_version.lambda_layers["matplotlib_layer"].arn,
      aws_lambda_layer_version.lambda_layers["numpy_layer"].arn,
      aws_lambda_layer_version.lambda_layers["pillow_layer"].arn,
      aws_lambda_layer_version.lambda_layers["xlsxwriter_layer"].arn,
      "arn:aws:lambda:us-east-1:336392948345:layer:AWSSDKPandas-Python313:4"
    ]
  )

  environment {
    variables = {
      S3_BUCKET            = aws_s3_bucket.securityhub_reports.bucket
      SES_SENDER_EMAIL     = var.ses_sender_email
      SES_RECIPIENT_EMAILS = var.ses_recipient_emails

      # Optional tuning
      HOURS     = 24
      PERIOD    = 300
      REGIONS   = "us-east-1,ap-southeast-1"
      S3_PREFIX = "cloudwatch/excel"
    }
  }

  tags = {
    Environment = var.env_name
    Purpose     = "CloudWatch Dashboard Excel Emailer"
  }
}
