variable "aws_region" {
  description = "The AWS region to deploy resources in"
  type        = string
  default     = "us-east-1"
}

variable "env_name" {
  description = "The environment name"
  type        = string
  default     = "prod"
}

# ------------------------
# Lambda Configuration
# ------------------------
variable "lambda_function_name" {
  description = "Lambda function name"
  type        = string
  default     = "cloudwatch-dashboard-emailer"
}

# ------------------------
# SES Configuration
# ------------------------
variable "ses_sender_email" {
  description = "Verified SES sender email address"
  type        = string
  default     = "chinthayadav6@gmail.com"
}

variable "ses_recipient_emails" {
  description = "Comma-separated list of recipient emails"
  type        = string
  default     = "chinnayya339@gmail.com"
}
