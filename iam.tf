# # ---------------------------
# # Trust Policy for Lambda
# # ---------------------------
# data "aws_iam_policy_document" "lambda_assume_role" {
#   statement {
#     actions = ["sts:AssumeRole"]

#     principals {
#       type        = "Service"
#       identifiers = ["lambda.amazonaws.com"]
#     }
#   }
# }

# resource "aws_iam_role" "lambda_exec_role" {
#   name               = "cwdashboards-role" # keep if you want; name can be changed
#   assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
# }

# # ---------------------------
# # Lambda Permissions Policy
# # ---------------------------
# data "aws_iam_policy_document" "lambda_policy" {
#   # CloudWatch Logs
#   statement {
#     effect = "Allow"
#     actions = [
#       "logs:CreateLogGroup",
#       "logs:CreateLogStream",
#       "logs:PutLogEvents"
#     ]
#     resources = ["arn:aws:logs:*:*:*"]
#   }

#   # CloudWatch Dashboards + Metric images
#   statement {
#     effect = "Allow"
#     actions = [
#       "cloudwatch:ListDashboards",
#       "cloudwatch:GetDashboard",
#       "cloudwatch:GetMetricWidgetImage"
#     ]
#     resources = ["*"]
#   }

#   # S3 store/read widget images (use your bucket resource)
#   statement {
#     effect = "Allow"
#     actions = [
#       "s3:PutObject",
#       "s3:GetObject"
#     ]
#     resources = ["${aws_s3_bucket.securityhub_reports.arn}/*"]
#   }

#   # SES send mail
#   statement {
#     effect = "Allow"
#     actions = [
#       "ses:SendRawEmail",
#       "ses:SendEmail"
#     ]
#     resources = ["*"]
#   }
# }

# # ---------------------------
# # Create IAM Policy
# # ---------------------------
# resource "aws_iam_policy" "lambda_policy" {
#   name   = "cwdashboards-policy" # name can be changed
#   policy = data.aws_iam_policy_document.lambda_policy.json
# }

# # ---------------------------
# # Attach Policy to Lambda Role
# # ---------------------------
# resource "aws_iam_role_policy_attachment" "lambda_attach" {
#   role       = aws_iam_role.lambda_exec_role.name
#   policy_arn = aws_iam_policy.lambda_policy.arn
# }

# ---------------------------
# Trust Policy for Lambda
# ---------------------------
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec_role" {
  name               = "cwdashboards-role" # name can be changed
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# ---------------------------
# Lambda Permissions Policy
# ---------------------------
data "aws_iam_policy_document" "lambda_policy" {
  # CloudWatch Logs
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = ["arn:aws:logs:*:*:*"]
  }

  # CloudWatch: discover metrics + render widget images
  statement {
    effect = "Allow"
    actions = [
      "cloudwatch:ListMetrics",
      "cloudwatch:GetMetricWidgetImage"
    ]
    resources = ["*"]
  }

  # S3: upload Excel workbooks
  statement {
    effect = "Allow"
    actions = [
      "s3:PutObject"
    ]
    resources = ["${aws_s3_bucket.securityhub_reports.arn}/*"]
  }

  # SES: send email with (optional) attachments
  statement {
    effect = "Allow"
    actions = [
      "ses:SendRawEmail"
    ]
    resources = ["*"]
  }
}

# ---------------------------
# Create IAM Policy
# ---------------------------
resource "aws_iam_policy" "lambda_policy" {
  name   = "cwdashboards-policy"
  policy = data.aws_iam_policy_document.lambda_policy.json
}

# ---------------------------
# Attach Policy to Lambda Role
# ---------------------------
resource "aws_iam_role_policy_attachment" "lambda_attach" {
  role       = aws_iam_role.lambda_exec_role.name
  policy_arn = aws_iam_policy.lambda_policy.arn
}

