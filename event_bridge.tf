# -------------------------------------------------
# CloudWatch Event Rule - Daily Schedule (9 AM UTC)
# -------------------------------------------------
resource "aws_cloudwatch_event_rule" "daily_cw_dashboard_rule" {
  name                = "daily_cw_dashboard_rule"
  description         = "Triggers Lambda daily at 9 AM UTC to email CloudWatch dashboards"
  schedule_expression = "cron(0 9 * * ? *)"
}

# -------------------------------------------
# Event Target - Connect Rule to Lambda
# -------------------------------------------
resource "aws_cloudwatch_event_target" "daily_lambda_target" {
  rule      = aws_cloudwatch_event_rule.daily_cw_dashboard_rule.name
  target_id = "cw_dashboard_daily_target"
  arn       = aws_lambda_function.cw_dashboard_emailer.arn
}

# ------------------------------------------------
# Lambda Permission - Allow EventBridge Invocation
# ------------------------------------------------
resource "aws_lambda_permission" "allow_eventbridge_invoke" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cw_dashboard_emailer.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_cw_dashboard_rule.arn
}