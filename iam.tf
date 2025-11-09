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
  name               = "cwdashboards-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# ---------------------------
# Lambda Permissions Policy (Full & Clean)
# ---------------------------
data "aws_iam_policy_document" "lambda_policy" {

  # CloudWatch Logs (for Lambda logging)
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = ["arn:aws:logs:*:*:*"]
  }

  # STS identity (useful for account info)
  statement {
    effect    = "Allow"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }

  # CloudWatch metrics read access
  statement {
    effect = "Allow"
    actions = [
      "cloudwatch:ListMetrics",
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricWidgetImage"
    ]
    resources = ["*"]
  }

  # S3 access for storing Excel dashboards
  statement {
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject"
    ]
    resources = ["${aws_s3_bucket.securityhub_reports.arn}/*"]
  }

  # SES for sending emails
  statement {
    effect = "Allow"
    actions = [
      "ses:SendEmail",
      "ses:SendRawEmail"
    ]
    resources = ["*"]
  }

  # ---------------------------
  # ECR: Required for Lambda Image-based Functions
  # ---------------------------
  statement {
    sid    = "AllowPullFromECR"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage"
    ]
    resources = ["*"]
  }
}

# ---------------------------
# Create and Attach Policy
# ---------------------------
resource "aws_iam_policy" "lambda_policy" {
  name   = "cwdashboards-policy"
  policy = data.aws_iam_policy_document.lambda_policy.json
}

resource "aws_iam_role_policy_attachment" "lambda_attach" {
  role       = aws_iam_role.lambda_exec_role.name
  policy_arn = aws_iam_policy.lambda_policy.arn
}

# ---------------------------
# Attach AWS Managed Policy for VPC Access (Optional)
# ---------------------------
# Only if your Lambda runs inside a VPC (e.g., EFS or private subnets)
resource "aws_iam_role_policy_attachment" "vpc_access" {
  role       = aws_iam_role.lambda_exec_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}
