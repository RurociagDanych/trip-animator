locals {
  vps_iam_user_policy_arns = var.vps_iam_user_name == "" ? {} : merge(
    { dispatcher = aws_iam_policy.dispatcher.arn },
    local.marked_trails_enabled ? { marked_trails_publisher = aws_iam_policy.marked_trails_publisher[0].arn } : {},
    local.marked_trails_builder_count == 1 ? { marked_trails_launcher = aws_iam_policy.marked_trails_launcher[0].arn } : {},
  )
}

resource "aws_iam_user_policy_attachment" "vps" {
  for_each = local.vps_iam_user_policy_arns

  user       = var.vps_iam_user_name
  policy_arn = each.value
}
